"""Explicit Windows Clash Verge adapter; no ambient proxies or global writes."""
from __future__ import annotations
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import stat
from .media_config import validate_attachment_proxy


class ProxyTransportError(ValueError):
    """Fixed diagnostics only; never controller secrets or response bodies."""


class _BoundedReader:
    def __init__(self, stream):
        self.stream, self.remaining = stream, 65536
    def _take(self, method, size):
        size = min(size if size >= 0 else self.remaining + 1, self.remaining + 1)
        value = method(size)
        self.remaining -= len(value)
        if self.remaining < 0:
            raise ProxyTransportError('Clash DNS response exceeds limit')
        return value
    def read(self, size=-1): return self._take(self.stream.read, size)
    def readline(self, size=-1): return self._take(self.stream.readline, size)
    def close(self): self.stream.close()
    def flush(self):
        # HTTPResponse.close flushes its reader; this adapter buffers no writes.
        pass


class _PipeSocket:
    def __init__(self, stream): self.stream = stream
    def makefile(self, *args): return _BoundedReader(self.stream)


def _secret(config: Path) -> str:
    try:
        for path in (config.parent, config):
            info = path.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError
        with config.open('rb') as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024: raise ValueError
        fields = re.findall(r'^secret:[ \t]*([^\r\n]*)\r?$', raw.decode('utf-8'), re.M)
        if len(fields) != 1: raise ValueError
        value = fields[0].strip()
        if value.startswith('"'):
            value = json.loads(value)
        elif value.startswith("'"):
            if not value.endswith("'"): raise ValueError
            value = value[1:-1].replace("''", "'")
        if not isinstance(value, str) or len(value) > 1024 or not value.isascii() or any(
                ord(char) <= 32 or ord(char) == 127 for char in value):
            raise ValueError
        return value
    except (OSError, ValueError, UnicodeError):
        raise ProxyTransportError('Clash local controller configuration unavailable') from None


class ClashResolver:
    def __init__(self, appdata: str):
        if os.name != 'nt' or not appdata or not Path(appdata).is_absolute():
            raise ProxyTransportError('Clash attachment proxy requires Windows Clash Verge')
        self.secret = _secret(Path(appdata) / 'io.github.clash-verge-rev.clash-verge-rev' / 'clash-verge.yaml')
        try:
            candidates = [name for name in os.listdir('\\\\.\\pipe\\') if re.fullmatch(
                r'verge-mihomo-production-[a-f0-9]{64}', name)]
        except OSError:
            raise ProxyTransportError('Clash local controller unavailable') from None
        if len(candidates) != 1:
            raise ProxyTransportError('Clash local controller must have one production pipe')
        self.pipe = '\\\\.\\pipe\\' + candidates[0]

    def addresses(self, host: str) -> list[str]:
        # Only validated ASCII DNS names, never an attachment path or signed URL.
        if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', host):
            raise ProxyTransportError('Invalid Clash DNS query')
        request = ('GET /dns/query?name=' + host + '&type=A HTTP/1.0\r\n'
                   'Host: localhost\r\nConnection: close\r\n')
        if self.secret:
            request += 'Authorization: Bearer ' + self.secret + '\r\n'
        try:
            with open(self.pipe, 'r+b', buffering=0) as stream:
                payload = memoryview((request + '\r\n').encode('ascii'))
                while payload:
                    count = stream.write(payload)
                    if not count: raise ProxyTransportError('Clash controller request failed')
                    payload = payload[count:]
                with http.client.HTTPResponse(_PipeSocket(stream)) as response:
                    response.begin()
                    if response.status != 200 or response.getheader('Content-Encoding', 'identity') != 'identity':
                        raise ProxyTransportError('Clash DNS query failed')
                    chunks = []
                    while chunk := response.read(4096):
                        chunks.append(chunk)
                    result = json.loads(b''.join(chunks))
            if (not isinstance(result, dict) or result.get('Status') != 0 or result.get('TC') is True or
                    not isinstance(result.get('Answer'), list) or len(result['Answer']) > 64):
                raise ValueError
            values = []
            for answer in result['Answer']:
                if not isinstance(answer, dict): raise ValueError
                if answer.get('type') in (1, 28):
                    value = answer.get('data')
                    if not isinstance(value, str): raise ValueError
                    values.append(value.strip())
            if not values: raise ValueError
            return values
        except (OSError, ValueError, UnicodeError, http.client.HTTPException):
            raise ProxyTransportError('Clash DNS query unavailable or invalid') from None


class ProxyHTTPSConnection(http.client.HTTPConnection):
    def __init__(self, host: str, address: str, timeout: float, proxy_url: str):
        proxy_host, proxy_port = validate_attachment_proxy(proxy_url)
        super().__init__(proxy_host, proxy_port, timeout=timeout)
        self.origin_host = host
        self.set_tunnel(address, 443)
        self._context = ssl.create_default_context()

    def connect(self):
        try:
            super().connect()
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.origin_host)
        except BaseException:
            self.close()
            raise
