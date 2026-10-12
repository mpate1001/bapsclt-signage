"""Publisher chunk publication to verified offline Pi cache, without large files or devices."""
import hashlib
import importlib.util
import io
import os
import re
import sys
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

# Only synthetic test credentials; no reboot helper or real remote host is contacted.
os.environ.setdefault('ADMIN_USERNAME', 'test-admin')
os.environ.setdefault('ADMIN_PASSWORD', 'test-password')
os.environ.setdefault('SESSION_SECRET', 's' * 40)
os.environ.setdefault('SIGNAGE_API_TOKEN', 't' * 40)
ROOT = Path(__file__).resolve().parents[1]
server_spec = importlib.util.spec_from_file_location('chunk_integration_server', ROOT / 'server/main.py')
server = importlib.util.module_from_spec(server_spec)
sys.modules[server_spec.name] = server
server_spec.loader.exec_module(server)
pi_spec = importlib.util.spec_from_file_location('chunk_integration_pi', ROOT / 'pi/signage-agent.py')
pi = importlib.util.module_from_spec(pi_spec)
pi_spec.loader.exec_module(pi)


def csrf(client):
    response = client.get('/upload')
    assert response.status_code == 200
    return re.search(r'const csrf = "([^\"]+)";', response.text).group(1)


def test_publisher_chunks_publish_and_pi_downloads_verified_video(tmp_path):
    settings = server.Settings('admin', 'secret-password', 's' * 40, 't' * 40,
                               tmp_path / 'server-data', 20, False,
                               'publisher', 'fake-publisher-password')
    with TestClient(server.create_app(settings)) as client:
        login = client.post('/upload/login', data={
            'username': 'publisher', 'password': 'fake-publisher-password', 'csrf': csrf(client)})
        assert login.status_code == 200
        headers = {'X-CSRF-Token': csrf(client)}
        # Just over the actual per-request chunk limit; proves assembly across chunks.
        body = b'synthetic-video-block' * 420_000
        expected = hashlib.sha256(body).hexdigest()
        created = client.post('/upload/uploads', json={'size': len(body), 'filename': 'signage.mp4'}, headers=headers)
        assert created.status_code == 201, created.text
        state = created.json()
        assert state['chunk_size'] == 8 * 1024 * 1024
        assert len(body) > state['chunk_size']
        upload_id = state['id']
        chunk_size = state['chunk_size']
        for index, start in enumerate(range(0, len(body), chunk_size)):
            response = client.post(f'/upload/uploads/{upload_id}/chunks/{index}',
                                   content=body[start:start + chunk_size],
                                   headers=headers | {'Content-Type': 'application/octet-stream'})
            assert response.status_code == 200, response.text
            assert response.json()['received'] == min(start + chunk_size, len(body))
        assert client.get('/public/current').json() == {'version': None}

        def validate_assembled(path):
            assert Path(path).read_bytes() == body

        with patch.object(server, 'validate_video', side_effect=validate_assembled) as validated:
            finalized = client.post(f'/upload/uploads/{upload_id}/complete', json={}, headers=headers)
            assert finalized.status_code == 202, finalized.text
            assert finalized.json()['status'] == 'finalizing'
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status = client.get(f'/upload/uploads/{upload_id}')
                assert status.status_code == 200
                state = status.json()
                if state['status'] in ('complete', 'failed'):
                    break
                time.sleep(0.01)
            assert state['status'] == 'complete', state
            assert state['version'] == expected
            validated.assert_called_once()
        public = client.get('/public/current').json()
        assert public['version'] == expected
        assert client.get(public['video_url']).content == body
        assert client.get('/api/current').status_code == 401

        class TestClientHTTPBridge:
            def open(self, request, timeout):
                # Exercise the existing Pi _open method's bearer request construction.
                assert request.get_header('Authorization') == 'Bearer ' + 't' * 40
                parsed = urlsplit(request.full_url)
                assert parsed.netloc == 'testserver'
                response = client.get(parsed.path + ('?' + parsed.query if parsed.query else ''),
                                      headers=dict(request.header_items()))
                assert response.status_code == 200, response.text
                return io.BytesIO(response.content)

        cache = pi.Cache(tmp_path / 'pi-cache', 'http://testserver', 't' * 40)
        with patch.object(pi, 'build_opener', return_value=TestClientHTTPBridge()):
            assert cache.update() is True
            assert cache.update() is False  # Published version does not redownload.
        assert cache.state()['version'] == expected
        cached = cache.video_path(expected)
        assert cached.stat().st_size == len(body)
        assert hashlib.sha256(cached.read_bytes()).hexdigest() == expected
        restarted = pi.Cache(tmp_path / 'pi-cache', 'http://testserver', 't' * 40)
        assert restarted.state() == cache.state()  # Offline startup sees the verified file.
