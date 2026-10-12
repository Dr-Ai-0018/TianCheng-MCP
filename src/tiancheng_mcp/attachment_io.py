"""Explicit direct attachment transport; signed URLs never reach argv or logs.

The worker streams to a parent-owned temporary descriptor. DNS is checked on
every redirect and the TLS connection uses the approved numeric address.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from urllib.parse import urljoin, urlsplit
from typing_extensions import NotRequired, TypedDict

from .jobs import JobCancelled
from .attachment_proxy import ClashResolver, ProxyHTTPSConnection, ProxyTransportError
from .media_config import validate_attachment_proxy


@dataclass(frozen=True)
class DownloadLimits:
    source_bytes: int = 32 * 1024 * 1024
    seconds: float = 60
    connect_seconds: float = 5
    redirects: int = 3


_SLOTS = threading.BoundedSemaphore(2)


class FileReference(TypedDict):
    download_url: str
    file_id: str
    mime_type: NotRequired[str]
    file_name: NotRequired[str]


def validate_reference(file: FileReference):
    if not isinstance(file, dict) or set(file) - {'download_url', 'file_id', 'mime_type', 'file_name'}:
        raise DownloadError('Invalid attachment reference')
    for name, maximum in (('download_url', 16384), ('file_id', 1024), ('mime_type', 256), ('file_name', 255)):
        value = file.get(name)
        if value is None and name in ('mime_type', 'file_name'):
            continue
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise DownloadError('Invalid attachment reference')


class DownloadError(ValueError):
    """Fixed messages only: never interpolate a signed URL or response body."""


class AttachmentDNSPolicyError(DownloadError):
    """Preserve actionable DNS diagnostics across the isolated worker boundary."""


def checked_url(url: str, hosts: tuple[str, ...]):
    if (not isinstance(url, str) or len(url) > 16384 or
            any(ord(char) <= 32 or ord(char) == 127 for char in url) or '\\' in url):
        raise DownloadError('Invalid attachment URL')
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise DownloadError('Invalid attachment URL') from None
    if (parts.scheme != 'https' or not host or host.lower() not in hosts or
            parts.username is not None or parts.password is not None or
            port not in (None, 443) or parts.fragment):
        raise DownloadError('Attachment URL is outside the allowed HTTPS hosts')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise DownloadError('Attachment URL must use an allowed DNS host')
    # ASCII DNS was validated in local settings. Path/query must be usable by
    # http.client without silently encoding a different signed request.
    target = (parts.path or '/') + ('?' + parts.query if parts.query else '')
    if not target.isascii():
        raise DownloadError('Attachment URL must be percent encoded')
    return host.lower(), target


def public_address(host: str) -> str:
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise DownloadError('Attachment DNS lookup failed') from None
    return checked_addresses([answer[4][0] for answer in answers])


def checked_addresses(values: list[str]) -> str:
    addresses = []
    for value in values:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise DownloadError('Attachment DNS returned an invalid address') from None
        mapped = getattr(address, 'ipv4_mapped', None)
        if (not address.is_global or address.is_multicast or address.is_reserved or
                getattr(address, 'scope_id', None) is not None or
                (mapped is not None and not mapped.is_global)):
            raise AttachmentDNSPolicyError('Attachment DNS must resolve only to public addresses')
        addresses.append(value)
    if not addresses:
        raise DownloadError('Attachment DNS returned no addresses')
    return addresses[0]


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: float):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Numeric address only; SNI and certificate validation use the original
        # host. No ambient proxy, cookies, credentials or alternate DNS lookup.
        family = socket.AF_INET6 if ':' in self.address else socket.AF_INET
        stream = socket.socket(family, socket.SOCK_STREAM)
        stream.settimeout(self.timeout)
        try:
            stream.connect((self.address, 443))
            self.sock = self._context.wrap_socket(stream, server_hostname=self.host)
        except BaseException:
            stream.close()
            raise


def stream_attachment(url: str, hosts: tuple[str, ...], output, limits: DownloadLimits, *,
                      proxy_url: str | None = None, controller_appdata: str = ''):
    checked_url(url, hosts)
    if proxy_url is not None:
        validate_attachment_proxy(proxy_url)
    resolver = ClashResolver(controller_appdata) if proxy_url is not None else None
    for hop in range(limits.redirects + 1):
        host, target = checked_url(url, hosts)
        address = checked_addresses(resolver.addresses(host)) if resolver else public_address(host)
        connection = (ProxyHTTPSConnection(host, address, limits.connect_seconds, proxy_url)
                      if resolver else PinnedHTTPSConnection(host, address, limits.connect_seconds))
        try:
            connection.request('GET', target, headers={'Host': host, 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location or hop == limits.redirects:
                    raise DownloadError('Attachment redirect limit exceeded')
                url = urljoin(url, location)
                continue
            if response.status in (401, 403, 404, 410):
                raise DownloadError('Attachment unavailable or expired; attach the file again')
            if response.status != 200:
                raise DownloadError('Attachment server did not return a file')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise DownloadError('Compressed attachment transport is not supported')
            lengths = response.headers.get_all('Content-Length') or []
            if len(lengths) > 1 or (lengths and not lengths[0].isdigit()):
                raise DownloadError('Invalid attachment content length')
            declared = int(lengths[0]) if lengths else None
            if declared is not None and declared > limits.source_bytes:
                raise DownloadError('Attachment exceeds byte limit')
            total = 0
            while chunk := response.read(65536):
                total += len(chunk)
                if total > limits.source_bytes:
                    raise DownloadError('Attachment exceeds byte limit')
                output.write(chunk)
            if declared is not None and total != declared:
                raise DownloadError('Attachment transfer was incomplete')
            return total
        finally:
            connection.close()
    raise DownloadError('Attachment redirect limit exceeded')


def download_attachment(url: str, hosts: tuple[str, ...], *, cancel: threading.Event | None = None,
                        limits: DownloadLimits = DownloadLimits(), proxy_url: str | None = None) -> bytes:
    checked_url(url, hosts)
    if proxy_url is not None:
        validate_attachment_proxy(proxy_url)
    deadline = time.monotonic() + limits.seconds
    def check():
        if cancel is not None and cancel.is_set():
            raise JobCancelled('Attachment download cancelled')
        if time.monotonic() >= deadline:
            raise TimeoutError('Attachment download deadline exceeded')
    while not _SLOTS.acquire(timeout=0.05):
        check()
    child = None
    try:
        check()
        with tempfile.TemporaryFile('w+b') as output:
            child = subprocess.Popen([sys.executable, '-I', '-m', 'tiancheng_mcp.attachment_io', '--worker'],
                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
                env={name: value for name, value in os.environ.items()
                     if name.upper() in {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'}},
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            request = {'url': url, 'hosts': hosts, 'limits': asdict(limits)}
            if proxy_url is not None:
                request.update(proxy_url=proxy_url, controller_appdata=os.environ.get('APPDATA', ''))
            payload = json.dumps(request).encode('utf-8')
            first = True
            while True:
                check()
                try:
                    child.communicate(input=payload if first else None, timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    first = False
            check()
            if child.returncode:
                messages = {10: 'Attachment source rejected by URL/DNS/size policy',
                            11: 'Attachment unavailable or expired; attach the file again',
                            13: 'Attachment DNS returned a non-public address (possibly proxy Fake-IP); download is unavailable',
                            14: 'Clash attachment proxy unavailable; check the local proxy, controller and TLS connection'}
                raise DownloadError(messages.get(child.returncode, 'Attachment download failed'))
            output.seek(0)
            data = output.read(limits.source_bytes + 1)
            if len(data) > limits.source_bytes:
                raise DownloadError('Attachment exceeds byte limit')
            return data
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate()
        _SLOTS.release()


def _worker():
    request = {}
    try:
        request = json.loads(sys.stdin.buffer.read(65537))
        options = {}
        if request.get('proxy_url') is not None:
            validate_attachment_proxy(request['proxy_url'])
            options = {'proxy_url': request['proxy_url'], 'controller_appdata': request.get('controller_appdata', '')}
        stream_attachment(request['url'], tuple(request['hosts']), sys.stdout.buffer,
                          DownloadLimits(**request['limits']), **options)
    except AttachmentDNSPolicyError:
        return 13
    except ProxyTransportError:
        return 14
    except DownloadError as exc:
        return 11 if 'expired' in str(exc) else 10
    except Exception:
        return 14 if isinstance(request, dict) and request.get('proxy_url') is not None else 12
    return 0


if __name__ == '__main__':
    raise SystemExit(_worker())
