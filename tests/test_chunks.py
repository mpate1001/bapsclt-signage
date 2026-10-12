"""Durable chunk protocol, access controls and publication recovery."""
import hashlib
import json
import re
import sqlite3
import threading
import time
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_server import server


@pytest.fixture
def settings(tmp_path):
    return server.Settings('admin', 'password', 's' * 40, 't' * 40, tmp_path,
                           2048, False, 'publisher', 'publisher-password', True)


def login(client, prefix='/admin'):
    username, password = ('admin', 'password') if prefix == '/admin' else ('publisher', 'publisher-password')
    token = re.search(r'const csrf = "([^"]+)";', client.get(prefix).text).group(1)
    assert client.post(prefix + '/login', data=dict(username=username, password=password, csrf=token)).status_code == 200
    token = re.search(r'const csrf = "([^"]+)";', client.get(prefix).text).group(1)
    return {'X-CSRF-Token': token}


def begin(client, headers, size=5, prefix='/admin'):
    response = client.post(prefix + '/uploads', json={'size': size, 'filename': '../../example.mp4'}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()['id']


def send(client, headers, identifier, body=b'video', index=0, prefix='/admin'):
    return client.post(f'{prefix}/uploads/{identifier}/chunks/{index}', content=body,
                       headers={**headers, 'Content-Type': 'application/octet-stream'})


def poll(client, identifier, prefix='/admin'):
    for _ in range(100):
        state = client.get(f'{prefix}/uploads/{identifier}').json()
        if state['status'] in ('complete', 'failed'):
            return state
        time.sleep(.01)
    pytest.fail('Finalization did not complete')


def test_identity_roles_csrf_and_metadata_limits(settings):
    app = server.create_app(settings)
    with TestClient(app) as client:
        assert client.post('/admin/uploads', json={'size': 5}).status_code == 401
        headers = login(client, '/upload')
        assert client.post('/admin/uploads', headers=headers, json={'size': 5}).status_code == 403
        assert client.post('/upload/uploads', json={'size': 5}).status_code == 403
        for bad in (True, 0, -1, '5'):
            assert client.post('/upload/uploads', headers=headers, json={'size': bad}).status_code == 400
        assert client.post('/upload/uploads', headers=headers, json={'size': 5, 'filename': 'x' * 256}).status_code == 400
        assert client.post('/upload/uploads', headers=headers, content=b'x' * 8193).status_code == 413
        identifier = begin(client, headers, 2147483648, '/upload')
        assert client.get(f'/upload/uploads/{identifier}').json()['size'] == 2147483648
        assert (settings.data_dir / 'uploads' / (identifier + '.part')).stat().st_size == 0
        assert client.post('/upload/uploads', headers=headers, json={'size': 2147483649}).status_code == 413
        client.cookies.clear()
        admin_headers = login(client)
        assert client.get(f'/admin/uploads/{identifier}').status_code == 404
        assert send(client, admin_headers, identifier).status_code == 404
        assert client.delete(f'/admin/uploads/{identifier}', headers=admin_headers).status_code == 404


def test_actual_stream_caps_duplicates_and_geometry(settings):
    with TestClient(server.create_app(settings)) as client:
        headers = login(client)
        identifier = begin(client, headers)
        binary_headers = {**headers, 'Content-Type': 'application/octet-stream'}
        response = client.post(f'/admin/uploads/{identifier}/chunks/0', content=iter([b'vid', b'eooo']), headers=binary_headers)
        assert response.status_code == 413
        assert client.get(f'/admin/uploads/{identifier}').json()['received'] == 0
        assert not list((settings.data_dir / 'uploads').glob('*.chunk'))
        assert send(client, headers, identifier, b'vid').status_code == 400
        assert send(client, headers, identifier, index=1).status_code == 409
        assert send(client, headers, identifier).json()['received'] == 5
        assert send(client, headers, identifier).json()['next_chunk'] == 1
        assert send(client, headers, identifier, b'other').status_code == 409
        assert client.post(f'/admin/uploads/{identifier}/complete', json={'bad': 1}, headers=headers).status_code == 400


def test_async_publish_restart_guard_and_validation_failure(settings):
    release, entered = threading.Event(), threading.Event()
    def validator(path):
        entered.set()
        release.wait(5)
    with TestClient(server.create_app(settings)) as client:
        headers = login(client)
        identifier = begin(client, headers)
        assert send(client, headers, identifier).status_code == 200
        with patch.object(server, 'validate_video', validator):
            response = client.post(f'/admin/uploads/{identifier}/complete', headers=headers, json={})
            assert response.status_code == 202
            assert entered.wait(2)
            assert client.post('/admin/restart', headers=headers).status_code == 409
            assert client.delete(f'/admin/uploads/{identifier}', headers=headers).status_code == 409
            release.set()
            state = poll(client, identifier)
        assert state['status'] == 'complete'
        assert state['version'] == hashlib.sha256(b'video').hexdigest()
        assert client.get(state['video_url']).content == b'video'
        assert client.post(f'/admin/uploads/{identifier}/complete', headers=headers).status_code == 200
        second = begin(client, headers, 4)
        send(client, headers, second, b'bad!')
        with patch.object(server, 'validate_video', side_effect=HTTPException(400, 'Invalid video.')):
            assert client.post(f'/admin/uploads/{second}/complete', headers=headers).status_code == 202
            failed = poll(client, second)
        assert failed['status'] == 'failed' and failed['error'] == 'Invalid video.'
        assert not (settings.data_dir / 'uploads' / (second + '.part')).exists()
        assert client.get('/public/current').json()['version'] == state['version']
        assert client.delete(f'/admin/uploads/{identifier}', headers=headers).status_code == 409
        assert client.delete(f'/admin/uploads/{second}', headers=headers).status_code == 200


def test_capacity_expiry_and_symlink_guard(settings, tmp_path):
    with TestClient(server.create_app(settings)) as client:
        headers = login(client)
        first, second = begin(client, headers), begin(client, headers)
        assert client.post('/admin/uploads', json={'size': 5}, headers=headers).status_code == 409
        path = settings.data_dir / 'uploads' / (first + '.part')
        path.unlink()
        target = tmp_path / 'untouched'
        target.write_bytes(b'important')
        path.symlink_to(target)
        assert send(client, headers, first).status_code == 409
        assert target.read_bytes() == b'important'
        assert client.delete(f'/admin/uploads/{first}', headers=headers).status_code == 200
        assert target.read_bytes() == b'important'
        with sqlite3.connect(settings.data_dir / 'devices.sqlite3') as db:
            db.execute('UPDATE uploads SET updated=? WHERE id=?', (time.time() - 86500, second))
        begin(client, headers)
        assert client.get(f'/admin/uploads/{second}').status_code == 404
        assert not (settings.data_dir / 'uploads' / (second + '.part')).exists()


def test_recovery_truncates_uncommitted_bytes_and_failed_short_storage(settings):
    with TestClient(server.create_app(settings)) as client:
        headers = login(client)
        identifier = begin(client, headers)
        path = settings.data_dir / 'uploads' / (identifier + '.part')
        path.write_bytes(b'uncommitted bytes')
    with TestClient(server.create_app(settings)) as client:
        headers = login(client)
        assert path.stat().st_size == 0
        assert send(client, headers, identifier).status_code == 200
        with sqlite3.connect(settings.data_dir / 'devices.sqlite3') as db:
            db.execute("UPDATE uploads SET status='finalizing' WHERE id=?", (identifier,))
    with TestClient(server.create_app(settings)) as client:
        login(client)
        assert client.get(f'/admin/uploads/{identifier}').json()['status'] == 'receiving'
    path.write_bytes(b'bad')
    with TestClient(server.create_app(settings)) as client:
        login(client)
        assert client.get(f'/admin/uploads/{identifier}').json()['status'] == 'failed'


def test_recovery_after_manifest_install(settings):
    with TestClient(server.create_app(settings)) as client:
        headers = login(client)
        identifier = begin(client, headers)
        send(client, headers, identifier)
        with patch.object(server, 'validate_video'):
            client.post(f'/admin/uploads/{identifier}/complete', headers=headers)
            state = poll(client, identifier)
        with sqlite3.connect(settings.data_dir / 'devices.sqlite3') as db:
            db.execute("UPDATE uploads SET status='finalizing' WHERE id=?", (identifier,))
    with TestClient(server.create_app(settings)) as client:
        login(client)
        assert client.get(f'/admin/uploads/{identifier}').json()['status'] == 'complete'


def test_default_and_explicit_limit(monkeypatch):
    monkeypatch.delenv('MAX_UPLOAD_MB', raising=False)
    assert server.Settings.from_env().max_upload_mb == 2048
    monkeypatch.setenv('MAX_UPLOAD_MB', '1024')
    assert server.Settings.from_env().max_upload_mb == 1024


def test_restart_scheduled_during_streamed_requests_prevents_mutation(settings):
    app = server.create_app(settings)
    with TestClient(app) as client:
        headers = login(client)
        def racing_body(body):
            yield body[:1]
            app.state.restart_task = object()
            yield body[1:]
        try:
            response = client.post('/admin/uploads', content=racing_body(b'{"size":5}'), headers=headers)
            assert response.status_code == 409
            assert not list((settings.data_dir / 'uploads').glob('*.part'))
        finally:
            app.state.restart_task = None
        identifier = begin(client, headers)
        try:
            response = client.post(f'/admin/uploads/{identifier}/chunks/0', content=racing_body(b'video'),
                                   headers={**headers, 'Content-Type': 'application/octet-stream'})
            assert response.status_code == 409
            assert client.get(f'/admin/uploads/{identifier}').json()['received'] == 0
        finally:
            app.state.restart_task = None
        send(client, headers, identifier)
        try:
            response = client.post(f'/admin/uploads/{identifier}/complete', content=racing_body(b'{}'), headers=headers)
            assert response.status_code == 409
            assert client.get(f'/admin/uploads/{identifier}').json()['status'] == 'receiving'
        finally:
            app.state.restart_task = None


def test_disk_write_failure_is_reconciled_before_retry(settings):
    with TestClient(server.create_app(settings)) as client:
        headers = login(client)
        identifier = begin(client, headers)
        original = server.os.fsync
        calls = 0
        def fail_append(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('simulated disk full')
            return original(fd)
        with patch.object(server.os, 'fsync', fail_append):
            assert send(client, headers, identifier).status_code == 507
        assert client.get(f'/admin/uploads/{identifier}').json()['received'] == 0
        assert send(client, headers, identifier).status_code == 200
        assert (settings.data_dir / 'uploads' / (identifier + '.part')).read_bytes() == b'video'


def test_shutdown_waits_for_validator_and_job_recovers(settings):
    entered, release = threading.Event(), threading.Event()
    def validator(path):
        entered.set()
        release.wait(5)
    with patch.object(server, 'validate_video', validator):
        with TestClient(server.create_app(settings)) as client:
            headers = login(client)
            identifier = begin(client, headers)
            send(client, headers, identifier)
            assert client.post(f'/admin/uploads/{identifier}/complete', headers=headers).status_code == 202
            assert entered.wait(2)
            # Lifespan shutdown cancels the job but waits for its bounded thread.
            timer = threading.Timer(.1, release.set)
            timer.start()
        timer.join()
    with TestClient(server.create_app(settings)) as client:
        headers = login(client)
        assert client.get(f'/admin/uploads/{identifier}').json()['status'] == 'receiving'
        with patch.object(server, 'validate_video'):
            client.post(f'/admin/uploads/{identifier}/complete', headers=headers)
            assert poll(client, identifier)['status'] == 'complete'
