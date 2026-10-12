"""Local-only media capabilities; attachment networking is explicit and off by default."""
from __future__ import annotations
import re
from urllib.parse import urlsplit
from dataclasses import dataclass


@dataclass(frozen=True)
class MediaSettings:
    images: bool = True
    exports: bool = False
    attachment_mode: str = 'off'
    attachment_hosts: tuple[str, ...] = ()
    attachment_proxy_url: str | None = None

    @classmethod
    def parse(cls, document=None):
        document = {} if document is None else document
        if not isinstance(document, dict) or set(document) - {'images', 'exports', 'attachments'}:
            raise ValueError('media must contain only images, exports and attachments')
        images, exports = document.get('images', True), document.get('exports', False)
        if not isinstance(images, bool) or not isinstance(exports, bool):
            raise ValueError('media images/exports must be booleans')
        attachment = document.get('attachments', {})
        if not isinstance(attachment, dict) or set(attachment) - {'mode', 'hosts', 'proxy_url'}:
            raise ValueError('media attachments must contain only mode, hosts and proxy_url')
        mode, hosts = attachment.get('mode', 'off'), attachment.get('hosts', [])
        if mode not in ('off', 'direct', 'clash_proxy') or not isinstance(hosts, list) or len(hosts) > 16:
            raise ValueError('Attachment mode must be off/direct/clash_proxy, with at most 16 exact hosts')
        accepted = []
        for host in hosts:
            if not isinstance(host, str) or len(host) > 253 or not re.fullmatch(
                r'(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}', host):
                raise ValueError('Attachment hosts must be exact DNS names, without wildcard, port or URL')
            accepted.append(host.lower())
        if mode != 'off' and not accepted:
            raise ValueError('Attachment downloads require at least one allowed host')
        proxy = attachment.get('proxy_url')
        if proxy is not None:
            validate_attachment_proxy(proxy)
        if mode == 'clash_proxy' and proxy is None:
            raise ValueError('Clash attachment downloads require an explicit loopback proxy URL')
        return cls(images, exports, mode, tuple(dict.fromkeys(accepted)), proxy)

    def document(self):
        attachment = {'mode': self.attachment_mode, 'hosts': list(self.attachment_hosts)}
        if self.attachment_proxy_url is not None:
            attachment['proxy_url'] = self.attachment_proxy_url
        return {'images': self.images, 'exports': self.exports, 'attachments': attachment}


def validate_attachment_proxy(value: str) -> tuple[str, int]:
    """Only numeric loopback HTTP endpoints; never proxy credentials or DNS."""
    try:
        if not isinstance(value, str) or len(value) > 128 or not value.isascii() or any(
                ord(char) <= 32 or ord(char) == 127 for char in value):
            raise ValueError
        parts = urlsplit(value)
        if (parts.scheme != 'http' or parts.hostname not in ('127.0.0.1', '::1') or
                parts.username is not None or parts.password is not None or
                parts.path not in ('', '/') or parts.query or parts.fragment or
                parts.port is None or not 1 <= parts.port <= 65535):
            raise ValueError
        return parts.hostname, parts.port
    except (ValueError, TypeError):
        raise ValueError('Attachment proxy must be an explicit credential-free loopback HTTP URL') from None
