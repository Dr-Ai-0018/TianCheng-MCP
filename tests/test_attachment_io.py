from __future__ import annotations
import io
import socket
import subprocess
import sys
import threading
from email.message import Message
import pytest
from tiancheng_mcp import attachment_io as module
from tiancheng_mcp.attachment_io import DownloadError, DownloadLimits
from tiancheng_mcp.jobs import JobCancelled

HOSTS = ('files.example.com',)
URL = 'https://files.example.com/file?signature=synthetic-secret'


@pytest.mark.parametrize('url', [
    'http://files.example.com/file', 'https://other.example.com/file',
    'https://files.example.com:444/file', 'https://user:secret@files.example.com/file',
    'https://127.0.0.1/file', 'https://files.example.com\\@localhost/file',
    'https://files.example.com/file\r\nAuthorization: secret',
])
def test_url_failures_do_not_echo_signed_input(url):
    with pytest.raises(DownloadError) as error: module.checked_url(url, HOSTS)
    assert url not in str(error.value)
    assert 'secret' not in str(error.value)


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254',
    '::1', 'fe80::1', '::ffff:127.0.0.1', '224.0.0.1', '100.64.0.1', '2001:db8::1', '198.18.0.111'])
def test_dns_requires_every_address_public(monkeypatch, address):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, '', ('8.8.8.8', 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 0, '', (address, 443))])
    with pytest.raises(module.AttachmentDNSPolicyError, match='public'): module.public_address(HOSTS[0])


def test_worker_preserves_dns_policy_diagnostic_without_signed_url(monkeypatch):
    import json
    from types import SimpleNamespace
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps({
        'url': URL, 'hosts': HOSTS, 'limits': module.asdict(DownloadLimits())}).encode())))
    def rejected(*args):
        raise module.AttachmentDNSPolicyError('Attachment DNS must resolve only to public addresses')
    monkeypatch.setattr(module, 'stream_attachment', rejected)
    assert module._worker() == 13


def test_dns_policy_worker_result_is_actionable_and_redacted(monkeypatch):
    original = subprocess.Popen
    def rejected(argv, **kwargs):
        return original([sys.executable, '-I', '-c', 'import sys; sys.stdin.buffer.read(); sys.exit(13)'], **kwargs)
    monkeypatch.setattr(module.subprocess, 'Popen', rejected)
    with pytest.raises(DownloadError, match='Fake-IP') as error:
        module.download_attachment(URL, HOSTS)
    assert URL not in str(error.value)
    assert 'synthetic-secret' not in str(error.value)


class Response:
    def __init__(self, status=200, data=b'original\0\xff', **headers):
        self.status, self.stream = status, io.BytesIO(data)
        self.headers = Message()
        for key, value in headers.items(): self.headers[key.replace('_', '-')] = value
    def getheader(self, name, default=None): return self.headers.get(name, default)
    def read(self, amount): return self.stream.read(amount)


def transport(monkeypatch, responses):
    addresses, requests, closed = [], [], []
    monkeypatch.setattr(module, 'public_address', lambda host: addresses.append(host) or '8.8.8.8')
    class Connection:
        def __init__(self, host, address, timeout):
            assert address == '8.8.8.8' and timeout == 5
        def request(self, method, target, headers): requests.append((method, target, headers))
        def getresponse(self): return responses.pop(0)
        def close(self): closed.append(True)
    monkeypatch.setattr(module, 'PinnedHTTPSConnection', Connection)
    return addresses, requests, closed


def test_stream_original_and_recheck_each_redirect(monkeypatch):
    addresses, requests, closed = transport(monkeypatch, [
        Response(302, Location='/next?token=synthetic'), Response(Content_Length='10')])
    output = io.BytesIO()
    assert module.stream_attachment(URL, HOSTS, output, DownloadLimits()) == 10
    assert output.getvalue() == b'original\0\xff'
    assert addresses == [HOSTS[0], HOSTS[0]] and len(closed) == 2
    assert requests[0][2] == {'Host': HOSTS[0], 'Accept-Encoding': 'identity'}


@pytest.mark.parametrize('response', [
    Response(302, Location='http://files.example.com/file'),
    Response(302, Location='https://other.example.com/file'),
    Response(Content_Encoding='gzip'), Response(Content_Length='99999999'),
    Response(Content_Length='12'), Response(403), Response(500),
])
def test_redirect_expiry_compression_and_size_failures(monkeypatch, response):
    _, _, closed = transport(monkeypatch, [response])
    with pytest.raises(DownloadError):
        module.stream_attachment(URL, HOSTS, io.BytesIO(), DownloadLimits())
    assert closed


def test_chunked_actual_limit_and_redirect_budget(monkeypatch):
    transport(monkeypatch, [Response(data=b'a'*11)])
    with pytest.raises(DownloadError, match='byte limit'):
        module.stream_attachment(URL, HOSTS, io.BytesIO(), DownloadLimits(source_bytes=10))
    transport(monkeypatch, [Response(302, Location='/next') for _ in range(4)])
    with pytest.raises(DownloadError, match='redirect limit'):
        module.stream_attachment(URL, HOSTS, io.BytesIO(), DownloadLimits())


def test_connection_pins_numeric_address_and_validates_original_hostname(monkeypatch):
    events = []
    class Stream:
        def settimeout(self, value): events.append(('timeout', value))
        def connect(self, target): events.append(('connect', target))
        def close(self): events.append(('close',))
    class Context:
        def wrap_socket(self, stream, *, server_hostname):
            events.append(('tls', server_hostname)); return stream
    connection = module.PinnedHTTPSConnection(HOSTS[0], '8.8.8.8', 5)
    monkeypatch.setattr(socket, 'socket', lambda *a: Stream())
    connection._context = Context()
    connection.connect()
    assert ('connect', ('8.8.8.8', 443)) in events
    assert ('tls', HOSTS[0]) in events


@pytest.mark.parametrize('cancelled', [False, True])
def test_real_stuck_worker_is_killed_and_signed_url_not_in_argv(monkeypatch, cancelled):
    original = subprocess.Popen
    children = []
    def stuck(argv, **kwargs):
        assert URL not in repr(argv)
        assert 'CLOUD_SYNTHETIC_KEY' not in kwargs['env']
        child = original([sys.executable, '-I', '-c', 'import time; time.sleep(60)'], **kwargs)
        children.append(child); return child
    monkeypatch.setattr(module.subprocess, 'Popen', stuck)
    monkeypatch.setenv('CLOUD_SYNTHETIC_KEY', 'not-for-workers')
    cancel = threading.Event()
    timer = threading.Timer(0.12, cancel.set) if cancelled else None
    if timer: timer.start()
    try:
        with pytest.raises(JobCancelled if cancelled else TimeoutError):
            module.download_attachment(URL, HOSTS, cancel=cancel, limits=DownloadLimits(seconds=.3))
    finally:
        if timer: timer.join()
    assert children and children[0].poll() is not None
