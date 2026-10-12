"""Proxy safety and lifecycle gates; all network traffic here is synthetic."""
import io
import json
import http.client
import subprocess
import sys
import threading
import pytest
from tiancheng_mcp import attachment_proxy as proxy
from tiancheng_mcp import attachment_io as transport
from tiancheng_mcp.media_config import MediaSettings, validate_attachment_proxy
from tiancheng_mcp.jobs import JobCancelled
from test_attachment_io import Response, HOSTS, URL


@pytest.mark.parametrize('value', [
    'http://localhost:7897', 'https://127.0.0.1:7897', 'socks5://127.0.0.1:7897',
    'http://user:secret@127.0.0.1:7897', 'http://192.168.1.1:7897',
    'http://127.0.0.1', 'http://127.0.0.1:0', 'http://127.0.0.1:65536',
    'http://127.0.0.1:7897/path', 'http://127.0.0.1:7897?token=secret',
    'http://127.0.0.1:7897#x', 'http://127.0.0.1:7897\r\n', None,
])
def test_proxy_settings_reject_ambient_remote_and_credential_urls(value):
    with pytest.raises(ValueError) as error: validate_attachment_proxy(value)
    assert 'secret' not in str(error.value)


def test_proxy_settings_require_local_explicit_endpoint():
    assert validate_attachment_proxy('http://[::1]:7897') == ('::1', 7897)
    value = {'attachments': {'mode': 'clash_proxy', 'hosts': list(HOSTS),
                             'proxy_url': 'http://127.0.0.1:7897'}}
    assert MediaSettings.parse(value).document()['attachments'] == value['attachments']
    for missing in ('hosts', 'proxy_url'):
        document = json.loads(json.dumps(value)); del document['attachments'][missing]
        with pytest.raises(ValueError): MediaSettings.parse(document)


def pipe_response(monkeypatch, body, status=200):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    received = []
    class Pipe:
        def __init__(self):
            self.input = io.BytesIO(b'HTTP/1.0 ' + str(status).encode() + b' OK\r\nContent-Length: ' +
                                    str(len(raw)).encode() + b'\r\n\r\n' + raw)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def write(self, payload): received.append(bytes(payload)); return len(payload)
        def readline(self, size=-1): return self.input.readline(size)
        def read(self, size=-1): return self.input.read(min(size, 7))
        def close(self): pass
    monkeypatch.setattr('builtins.open', lambda *a, **k: Pipe())
    resolver = object.__new__(proxy.ClashResolver)
    resolver.secret, resolver.pipe = 'synthetic-controller-secret', 'synthetic-local-pipe'
    return resolver, received


def test_controller_query_is_bounded_and_contains_only_dns_name(monkeypatch):
    resolver, requests = pipe_response(monkeypatch, {'Status': 0, 'Answer': [
        {'type': 5, 'data': 'alias.example.com.'}, {'type': 1, 'data': ' 8.8.8.8 '}]})
    assert resolver.addresses(HOSTS[0]) == ['8.8.8.8']
    request = b''.join(requests)
    assert b'GET /dns/query?name=files.example.com&type=A ' in request
    assert b'Authorization: Bearer synthetic-controller-secret' in request
    assert b'synthetic-secret' not in request and b'signature=' not in request


@pytest.mark.parametrize('body,status', [
    ({'Status': 3, 'Answer': []}, 200), ({'Status': 0, 'TC': True, 'Answer': []}, 200),
    ({'Status': 0, 'Answer': [{'type': 1, 'data': None}]}, 200),
    ({'Status': 0, 'Answer': [{'type': 1, 'data': '8.8.8.8'}] * 65}, 200),
    (b'synthetic-controller-secret not json', 200), (b'synthetic-controller-secret', 401),
    (b'x' * 66000, 200),
], ids=['rcode', 'truncated', 'invalid-record', 'too-many-records', 'invalid-json', 'unauthorized', 'oversized'])
def test_controller_failures_are_redacted(monkeypatch, body, status):
    resolver, _ = pipe_response(monkeypatch, body, status)
    with pytest.raises(proxy.ProxyTransportError) as error: resolver.addresses(HOSTS[0])
    assert 'synthetic-controller-secret' not in str(error.value)


@pytest.mark.parametrize('content', [
    'secret: "synthetic-controller-secret\\r\\nInjected"', 'secret: one\nsecret: two',
    'secret: "unterminated', 'secret: ' + 'x' * 1025,
], ids=['header-injection', 'duplicate-secret', 'unclosed-quote', 'oversized-secret'])
def test_controller_config_rejects_ambiguous_or_injectable_secrets(tmp_path, content):
    config = tmp_path/'clash-verge.yaml'; config.write_text(content)
    with pytest.raises(proxy.ProxyTransportError) as error: proxy._secret(config)
    assert 'synthetic-controller-secret' not in str(error.value)


def test_controller_config_is_bounded_and_plain_or_quoted(tmp_path):
    config = tmp_path/'clash-verge.yaml'
    for value in ('secret: token', 'secret: "token"', "secret: 'token'"):
        config.write_text(value)
        assert proxy._secret(config) == 'token'
    config.write_bytes(b'secret: token\n' + b'x' * (1024*1024))
    with pytest.raises(proxy.ProxyTransportError): proxy._secret(config)


def test_proxy_pins_ip_and_tls_uses_origin_not_proxy(monkeypatch):
    events = []
    class Stream:
        def close(self): events.append('closed')
    def connect(self):
        events.append((self.host, self.port, self._tunnel_host, self._tunnel_port))
        self.sock = Stream()
    class Context:
        def wrap_socket(self, stream, server_hostname):
            events.append(('tls', server_hostname)); return stream
    monkeypatch.setattr(http.client.HTTPConnection, 'connect', connect)
    connection = proxy.ProxyHTTPSConnection(HOSTS[0], '8.8.8.8', 5, 'http://127.0.0.1:7897')
    connection._context = Context()
    connection.connect()
    assert events == [('127.0.0.1', 7897, '8.8.8.8', 443), ('tls', HOSTS[0])]
    def rejected(*a, **k): raise OSError('synthetic TLS rejection')
    monkeypatch.setattr(connection._context, 'wrap_socket', rejected)
    with pytest.raises(OSError): connection.connect()
    assert 'closed' in events


def test_proxy_rechecks_redirect_and_keeps_credentials_out_of_origin(monkeypatch):
    queries, requests = [], []
    class Resolver:
        def __init__(self, *a): pass
        def addresses(self, host): queries.append(host); return ['8.8.8.8']
    responses = [Response(302, Location='/next'), Response(data=b'original')]
    class Connection:
        def __init__(self, host, address, timeout, url): assert address == '8.8.8.8'
        def request(self, method, target, headers): requests.append(headers)
        def getresponse(self): return responses.pop(0)
        def close(self): pass
    monkeypatch.setattr(transport, 'ClashResolver', Resolver)
    monkeypatch.setattr(transport, 'ProxyHTTPSConnection', Connection)
    monkeypatch.setattr(transport, 'public_address', lambda *a: pytest.fail('direct fallback'))
    output = io.BytesIO()
    transport.stream_attachment(URL, HOSTS, output, transport.DownloadLimits(), proxy_url='http://127.0.0.1:7897')
    assert output.getvalue() == b'original' and queries == [HOSTS[0], HOSTS[0]]
    assert requests == [{'Host': HOSTS[0], 'Accept-Encoding': 'identity'}] * 2
    responses.append(Response(302, Location='https://localhost/secret'))
    with pytest.raises(transport.DownloadError):
        transport.stream_attachment(URL, HOSTS, io.BytesIO(), transport.DownloadLimits(), proxy_url='http://127.0.0.1:7897')
    assert len(queries) == 3


def test_proxy_rejects_mixed_fake_ip_before_connect_and_never_falls_back(monkeypatch):
    class Resolver:
        def __init__(self, *a): pass
        def addresses(self, host): return ['8.8.8.8', '198.18.0.111']
    monkeypatch.setattr(transport, 'ClashResolver', Resolver)
    monkeypatch.setattr(transport, 'ProxyHTTPSConnection', lambda *a: pytest.fail('unsafe connect'))
    monkeypatch.setattr(transport, 'public_address', lambda *a: pytest.fail('direct fallback'))
    with pytest.raises(transport.AttachmentDNSPolicyError):
        transport.stream_attachment(URL, HOSTS, io.BytesIO(), transport.DownloadLimits(), proxy_url='http://127.0.0.1:7897')


def test_controller_unavailable_never_uses_direct_dns(monkeypatch):
    def unavailable(*args): raise proxy.ProxyTransportError('Clash local controller unavailable')
    monkeypatch.setattr(transport, 'ClashResolver', unavailable)
    monkeypatch.setattr(transport, 'public_address', lambda *a: pytest.fail('direct fallback'))
    with pytest.raises(proxy.ProxyTransportError):
        transport.stream_attachment(URL, HOSTS, io.BytesIO(), transport.DownloadLimits(), proxy_url='http://127.0.0.1:7897')


def test_invalid_source_rejected_before_controller_read(monkeypatch):
    monkeypatch.setattr(transport, 'ClashResolver', lambda *a: pytest.fail('controller read'))
    with pytest.raises(transport.DownloadError):
        transport.stream_attachment('https://localhost/secret', HOSTS, io.BytesIO(), transport.DownloadLimits(),
                                    proxy_url='http://127.0.0.1:7897')


def test_proxy_worker_error_is_redacted(monkeypatch):
    original = subprocess.Popen
    def rejected(argv, **kwargs):
        return original([sys.executable, '-I', '-c', 'import sys; sys.stdin.buffer.read(); sys.exit(14)'], **kwargs)
    monkeypatch.setattr(transport.subprocess, 'Popen', rejected)
    with pytest.raises(transport.DownloadError, match='Clash') as error:
        transport.download_attachment(URL, HOSTS, proxy_url='http://127.0.0.1:7897')
    assert URL not in str(error.value) and 'synthetic-secret' not in str(error.value)


@pytest.mark.parametrize('cancelled', [True, False])
def test_blocked_proxy_worker_is_killed_without_secret_env(monkeypatch, cancelled):
    original, children = subprocess.Popen, []
    def stuck(argv, **kwargs):
        assert URL not in repr(argv) and 'APPDATA' not in kwargs['env']
        assert 'HTTP_PROXY' not in kwargs['env']
        child = original([sys.executable, '-I', '-c', 'import time; time.sleep(60)'], **kwargs)
        children.append(child); return child
    monkeypatch.setattr(transport.subprocess, 'Popen', stuck)
    cancel = threading.Event()
    timer = threading.Timer(.1, cancel.set) if cancelled else None
    if timer: timer.start()
    try:
        with pytest.raises(JobCancelled if cancelled else TimeoutError):
            transport.download_attachment(URL, HOSTS, proxy_url='http://127.0.0.1:7897',
                cancel=cancel, limits=transport.DownloadLimits(seconds=.3))
    finally:
        if timer: timer.join()
    assert children and children[0].poll() is not None


@pytest.mark.parametrize('status', [200, 401])
def test_controller_http_response_is_closed_before_return(monkeypatch, status):
    resolver, _ = pipe_response(monkeypatch, {'Status': 0, 'Answer': [
        {'type': 1, 'data': '8.8.8.8'}]}, status)
    responses = []
    original = http.client.HTTPResponse
    class TrackedResponse(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            responses.append(self)
    monkeypatch.setattr(http.client, 'HTTPResponse', TrackedResponse)
    try:
        if status == 200:
            assert resolver.addresses(HOSTS[0]) == ['8.8.8.8']
        else:
            with pytest.raises(proxy.ProxyTransportError):
                resolver.addresses(HOSTS[0])
        assert responses and responses[0].closed
    finally:
        # Explicit cleanup keeps a failing regression from leaking at test exit.
        for response in responses:
            if not response.closed:
                if response.fp is not None:
                    response.fp.flush = lambda: None
                response.close()
