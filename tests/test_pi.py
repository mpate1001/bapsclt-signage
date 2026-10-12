import hashlib
import importlib.util
import io
import json
import threading
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

spec = importlib.util.spec_from_file_location('signage_agent', Path(__file__).resolve().parents[1] / 'pi/signage-agent.py')
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


def metadata(data):
    version = hashlib.sha256(data).hexdigest()
    return {'version': version, 'sha256': version, 'size': len(data), 'video_url': '/api/video?version=' + version}


def update(cache, data, description=None):
    with patch.object(cache, '_open', side_effect=[io.BytesIO(json.dumps(description or metadata(data)).encode()), io.BytesIO(data)]):
        return cache.update()


def test_verified_download_and_offline_restart(tmp_path):
    cache = agent.Cache(tmp_path, 'https://example.com', 'secret')
    assert cache.state() == {'version': None}
    assert update(cache, b'first-video')
    version = metadata(b'first-video')['version']
    assert cache.state()['version'] == version
    restarted = agent.Cache(tmp_path, 'https://example.com', 'secret')
    with patch.object(restarted, '_open', side_effect=OSError('offline')):
        with pytest.raises(OSError):
            restarted.update()
    assert restarted.state()['version'] == version
    assert restarted.video_path(version).read_bytes() == b'first-video'


def test_hash_mismatch_keeps_previous_state(tmp_path):
    cache = agent.Cache(tmp_path, 'https://example.com', 'secret')
    update(cache, b'previous')
    previous = cache.state()
    with pytest.raises(ValueError):
        update(cache, b'wrong-data', metadata(b'right-data'))
    assert cache.state() == previous
    assert not list(tmp_path.glob('.download-*'))
    assert not cache.video_path(metadata(b'right-data')['version']).exists()


def test_atomic_publication_only_after_download(tmp_path):
    cache = agent.Cache(tmp_path, 'https://example.com', 'secret')
    update(cache, b'previous')
    previous = cache.state()
    class Watched(io.BytesIO):
        def read(self, size=-1):
            assert cache.state() == previous
            return super().read(size)
    data = b'new video'
    with patch.object(cache, '_open', side_effect=[io.BytesIO(json.dumps(metadata(data)).encode()), Watched(data)]):
        cache.update()
    assert cache.state()['version'] == metadata(data)['version']
    assert not list(tmp_path.glob('.state-*'))


def test_no_remote_video_preserves_cache(tmp_path):
    cache = agent.Cache(tmp_path, 'https://example.com', 'secret')
    with patch.object(cache, '_open', return_value=io.BytesIO(b'{"version":null}')):
        assert cache.update() is False
    assert cache.state() == {'version': None}
    update(cache, b'previous')
    previous = cache.state()
    with patch.object(cache, '_open', return_value=io.BytesIO(b'{"version":null}')):
        cache.update()
    assert cache.state() == previous


@pytest.mark.parametrize('mutation', [
    {'video_url': 'https://evil.example/api/video'}, {'sha256': '0'*64}, {'size': True}, {'size': 0}, {'size': 1001}
])
def test_invalid_metadata_refused_before_download(tmp_path, mutation):
    cache = agent.Cache(tmp_path, 'https://example.com', 'secret', max_bytes=1000)
    description = metadata(b'video') | mutation
    with patch.object(cache, '_open', return_value=io.BytesIO(json.dumps(description).encode())) as opened:
        with pytest.raises(ValueError):
            cache.update()
        assert opened.call_count == 1
    assert cache.state() == {'version': None}


def test_same_origin_redirect_only():
    redirect = agent.SameOriginRedirect()
    request = Request('https://example.com/api/current', headers={'Authorization': 'Bearer secret'})
    with pytest.raises(ValueError):
        redirect.redirect_request(request, None, 302, 'Redirect', {}, 'https://other.example/api/current')
    assert redirect.redirect_request(request, None, 302, 'Redirect', {}, 'https://example.com/api/current').get_header('Authorization') == 'Bearer secret'


def test_remote_metadata_and_video_send_player_identity(tmp_path):
    data = b'video download'
    description = metadata(data)
    received = []

    class Remote(agent.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            received.append((self.path, self.headers.get('User-Agent'), self.headers.get('Authorization')))
            body = json.dumps(description).encode() if self.path == '/api/current' else data
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = agent.ThreadingHTTPServer(('127.0.0.1', 0), Remote)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        cache = agent.Cache(tmp_path, 'http://127.0.0.1:' + str(server.server_port), 'test-token')
        assert cache.update()
        assert received == [
            ('/api/current', 'BAPSCLT-Signage/1.0', 'Bearer test-token'),
            (description['video_url'], 'BAPSCLT-Signage/1.0', 'Bearer test-token'),
        ]
        assert cache.video_path(description['version']).read_bytes() == data
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.fixture
def local_server(tmp_path):
    cache = agent.Cache(tmp_path / 'data', 'https://example.com', 'secret')
    data = b'0123456789'
    update(cache, data)
    index = tmp_path / 'index.html'
    index.write_text('<html>player</html>')
    server = agent.ThreadingHTTPServer(('127.0.0.1', 0), agent.handler_for(cache, index))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield 'http://127.0.0.1:' + str(server.server_port), cache
    server.shutdown()
    server.server_close()
    worker.join()


def test_local_http_ranges_head_state_and_no_secrets(local_server):
    base, cache = local_server
    video = base + '/video?version=' + cache.state()['version']
    with urlopen(Request(video, headers={'Range': 'bytes=2-5'})) as response:
        assert response.status == 206
        assert response.headers['Content-Range'] == 'bytes 2-5/10'
        assert response.read() == b'2345'
    with urlopen(Request(video, headers={'Range': 'bytes=-3'})) as response:
        assert response.read() == b'789'
    with urlopen(Request(video, headers={'Range': 'bytes=8-'})) as response:
        assert response.read() == b'89'
    with urlopen(Request(video, method='HEAD')) as response:
        assert response.headers['Content-Length'] == '10'
        assert response.read() == b''
    with urlopen(base + '/state.json') as response:
        assert json.load(response) == cache.state()
    for path in ('/pi.env', '/signage-agent.py', '/../../pi.env', '/video?version=../../secret'):
        with pytest.raises(HTTPError) as caught:
            urlopen(base + path)
        assert caught.value.code == 404
    with pytest.raises(HTTPError) as caught:
        urlopen(Request(video, headers={'Range': 'bytes=15-20'}))
    assert caught.value.code == 416
    assert caught.value.headers['Content-Range'] == 'bytes */10'


def test_device_json_requests_authenticated_and_bounded(tmp_path):
    cache = agent.Cache(tmp_path, 'https://example.com', 'secret')
    controls = agent.RemoteControls(cache, 'pi-01')
    with patch.object(agent, 'build_opener') as opener:
        opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"ok":true}'
        assert controls.request('/heartbeat', {'cached_version': None, 'reboot_supported': False}) == {'ok': True}
        request = opener.return_value.open.call_args.args[0]
        assert request.get_method() == 'POST'
        assert request.get_header('Authorization') == 'Bearer secret'
        assert request.get_header('User-agent') == 'BAPSCLT-Signage/1.0'
        assert request.get_header('Content-type') == 'application/json'
        assert json.loads(request.data)['cached_version'] is None
        assert opener.return_value.open.call_args.kwargs['timeout'] == 30
        assert isinstance(opener.call_args.args[0], agent.SameOriginRedirect)
    with patch.object(cache, '_open', return_value=io.BytesIO(b'x' * 8193)):
        with pytest.raises(ValueError, match='limit'):
            controls.request('/commands')


def test_heartbeat_tracks_cache_without_helper(tmp_path):
    cache = agent.Cache(tmp_path, 'https://example.com', 'secret')
    update(cache, b'cached')
    controls = agent.RemoteControls(cache, 'pi-01')
    with patch.object(controls, 'supported', return_value=False), patch.object(controls, 'request', return_value={'ok': True}) as request:
        controls.sync()
    request.assert_called_once_with('/heartbeat', {'cached_version': metadata(b'cached')['version'], 'reboot_supported': False})


def test_reboot_ack_then_durable_history_prevents_replay(tmp_path):
    cache = agent.Cache(tmp_path, 'https://example.com', 'secret')
    controls = agent.RemoteControls(cache, 'pi-01')
    command = {'id': 'abc-123', 'type': 'reboot', 'expires_at': agent.time.time() + 120}
    def response(suffix, body=None):
        if suffix.endswith('/ack'):
            assert not controls.marker.exists()
        return {'commands': [command]} if suffix == '/commands' else {'ok': True}
    with patch.object(controls, 'supported', return_value=True), patch.object(controls, 'request', side_effect=response):
        controls.sync()
    assert controls.marker.read_text().strip() == command['id']
    assert command['id'] in json.loads(controls.history_path.read_text())
    controls.marker.unlink()  # Simulate fixed helper consuming request; never execute it.
    restarted = agent.RemoteControls(cache, 'pi-01')
    with patch.object(restarted, 'supported', return_value=True), patch.object(restarted, 'request', side_effect=response) as request:
        restarted.sync()
        assert request.call_count == 2
    assert not restarted.marker.exists()


@pytest.mark.parametrize('mutation', [
    {'type': 'shell'}, {'expires_at': 0}, {'expires_at': True}, {'expires_at': float('inf')},
    {'id': '../reboot'}, {'id': None}, {'expires_at': agent.time.time() + 99999},
])
def test_invalid_commands_never_ack_or_write(tmp_path, mutation):
    controls = agent.RemoteControls(agent.Cache(tmp_path, 'https://example.com', 'secret'), 'pi-01')
    command = {'id': 'safe-id', 'type': 'reboot', 'expires_at': agent.time.time() + 120} | mutation
    with patch.object(controls, 'supported', return_value=True), patch.object(controls, 'request', side_effect=[{'ok': True}, {'commands': [command]}]) as request:
        controls.sync()
        assert request.call_count == 2
    assert not controls.marker.exists()


def test_failed_ack_and_stale_marker_never_reboot(tmp_path):
    controls = agent.RemoteControls(agent.Cache(tmp_path, 'https://example.com', 'secret'), 'pi-01')
    command = {'id': 'safe-id', 'type': 'reboot', 'expires_at': agent.time.time() + 120}
    with patch.object(controls, 'supported', return_value=True), patch.object(controls, 'request', side_effect=[{'ok': True}, {'commands': [command]}, OSError('offline')]):
        with pytest.raises(OSError):
            controls.sync()
    assert not controls.marker.exists()
    assert not controls.history_path.exists()
    controls.marker.write_text('stale')
    with patch.object(controls, 'supported', return_value=True), patch.object(controls, 'request', side_effect=[{'ok': True}, {'commands': [command]}]) as request:
        controls.sync()
        assert request.call_count == 2
    assert controls.marker.read_text() == 'stale'


def test_control_failures_do_not_stop_poll_or_cached_playback(tmp_path):
    cache = agent.Cache(tmp_path, 'https://example.com', 'secret')
    update(cache, b'cached')
    stop = threading.Event()
    controls = agent.RemoteControls(cache, 'pi-01')
    def failure():
        stop.set()
        raise OSError('offline')
    with patch.object(cache, 'update', side_effect=OSError('offline')) as updater, patch.object(controls, 'sync', side_effect=failure):
        agent.poll(cache, 1, stop, controls)
    updater.assert_called_once()
    assert cache.state()['version'] == metadata(b'cached')['version']


def test_helper_capability_is_bounded_and_units_are_narrow(tmp_path):
    controls = agent.RemoteControls(agent.Cache(tmp_path, 'https://example.com', 'secret'), 'pi-01')
    with patch.object(agent.subprocess, 'run') as run:
        run.return_value.returncode = 0
        assert controls.supported()
        assert run.call_args.args[0] == ['/usr/bin/systemctl', 'is-active', '--quiet', 'bapsclt-signage-reboot.path']
        assert run.call_args.kwargs['timeout'] == 3
    with patch.object(agent.subprocess, 'run', side_effect=agent.subprocess.TimeoutExpired('systemctl', 3)):
        assert not controls.supported()
    root = Path(__file__).resolve().parents[1] / 'pi/systemd'
    assert 'PathExists=/home/admin/bapsclt-signage-data/reboot.request' in (root / 'bapsclt-signage-reboot.path').read_text()
    unit = (root / 'bapsclt-signage-reboot.service').read_text()
    assert 'ExecStartPre=/usr/bin/rm -f /home/admin/bapsclt-signage-data/reboot.request' in unit
    assert 'ExecStart=/usr/bin/systemctl reboot' in unit
    assert 'User=root' in unit
    assert 'NoNewPrivileges=true' in (root / 'bapsclt-signage-agent.service').read_text()


def test_default_limit_is_two_gib_without_allocating_video(tmp_path):
    cache = agent.Cache(tmp_path, 'https://example.com', 'secret')
    assert cache.max_bytes == 2_147_483_648
    description = metadata(b'video') | {'size': 2_147_483_648}
    with patch.object(cache, '_open', side_effect=[io.BytesIO(json.dumps(description).encode()), OSError('simulated outage')]) as opened:
        with pytest.raises(OSError):
            cache.update()
        assert opened.call_count == 2  # The exact upper boundary passes metadata validation.
    assert cache.state() == {'version': None}
    assert not list(tmp_path.glob('.download-*'))
    description['size'] += 1
    with patch.object(cache, '_open', return_value=io.BytesIO(json.dumps(description).encode())) as opened:
        with pytest.raises(ValueError):
            cache.update()
        assert opened.call_count == 1  # Above the cap is refused before download.


@pytest.mark.parametrize('configured, expected', [(None, 2_147_483_648), ('2000000000', 2_000_000_000)])
def test_startup_default_and_existing_explicit_limit(monkeypatch, configured, expected):
    monkeypatch.setenv('SIGNAGE_API_TOKEN', 'fake-test-token')
    monkeypatch.setenv('SIGNAGE_URL', 'https://example.com')
    monkeypatch.setenv('SIGNAGE_POLL_SECONDS', '30')
    if configured is None:
        monkeypatch.delenv('SIGNAGE_MAX_VIDEO_BYTES', raising=False)
    else:
        monkeypatch.setenv('SIGNAGE_MAX_VIDEO_BYTES', configured)
    # Stop before HTTP server, heartbeat, or reboot support starts.
    with patch.object(agent, 'Cache', side_effect=RuntimeError('captured config')) as constructor:
        with pytest.raises(RuntimeError, match='captured config'):
            agent.main()
        assert constructor.call_args.args[3] == expected
