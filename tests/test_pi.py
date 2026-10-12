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
