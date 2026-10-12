"""Admin controls are role protected, persistent and narrowly scoped."""
import hashlib
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from test_server import server, csrf, sign_in, publish, publisher_sign_in, client, publisher_client

TOKEN = {"Authorization": "Bearer " + "t" * 40}
OLD = hashlib.sha256(b"old").hexdigest()
NEW = hashlib.sha256(b"new").hexdigest()


def beat(client, device="pi-one", version=None, supported=True):
    return client.post(f"/api/devices/{device}/heartbeat", headers=TOKEN,
                       json={"cached_version": version, "reboot_supported": supported})


def queue(client, token, device="pi-one"):
    return client.post(f"/admin/devices/{device}/reboot", headers={"X-CSRF-Token": token})


def test_admin_auth_and_csrf(client, publisher_client):
    reads = ["/admin/library", "/admin/devices", "/admin/status"]
    writes = [f"/admin/videos/{OLD}/delete", "/admin/devices/pi-one/reboot", "/admin/restart"]
    for path in reads:
        assert client.get(path).status_code == 401
    for path in writes:
        assert client.post(path).status_code == 401
    token = publisher_sign_in(publisher_client)
    for path in reads:
        assert publisher_client.get(path).status_code == 403
    for path in writes:
        assert publisher_client.post(path, headers={"X-CSRF-Token": token}).status_code == 403
    sign_in(client)
    for path in writes:
        assert client.post(path).status_code == 403
    for path in reads:
        assert client.get(path).status_code == 200


def test_library_and_safe_deletion(client, tmp_path):
    token = sign_in(client)
    with patch.object(server, "validate_video"):
        assert publish(client, token, b"old").status_code == 200
        assert publish(client, token, b"new").status_code == 200
    videos = client.data_dir / "videos"
    (videos / ("a" * 64 + ".mp4")).symlink_to(videos / (NEW + ".mp4"))
    (videos / "not-a-version.mp4").write_bytes(b"ignore")
    assert len(client.get("/admin/library").json()["videos"]) == 2
    delete = lambda version: client.post(f"/admin/videos/{version}/delete", headers={"X-CSRF-Token": token})
    assert delete(NEW).status_code == 409
    assert delete("a" * 64).status_code == 404
    assert beat(client, version=OLD).status_code == 200
    library = {v["version"]: v for v in client.get("/admin/library").json()["videos"]}
    assert library[OLD]["in_use_by"] == ["pi-one"]
    assert library[NEW]["is_current"]
    assert delete(OLD).status_code == 409
    with sqlite3.connect(client.data_dir / "devices.sqlite3") as db:
        db.execute("UPDATE devices SET last_seen=?", (time.time() - 91,))
    assert delete(OLD).json() == {"ok": True}
    assert not (videos / (OLD + ".mp4")).exists()
    assert client.get("/public/current").json()["version"] == NEW
    assert delete(OLD).status_code == 404
    assert delete("invalid").status_code == 400


def test_delete_waits_for_publication_and_rechecks_current(client):
    token = sign_in(client)
    with patch.object(server, "validate_video"):
        publish(client, token, b"old")
        publish(client, token, b"new")
    entered, release = threading.Event(), threading.Event()
    def validate(_):
        entered.set()
        assert release.wait(5)
    with patch.object(server, "validate_video", side_effect=validate), ThreadPoolExecutor(2) as pool:
        uploading = pool.submit(publish, client, token, b"old")
        assert entered.wait(3)
        deleting = pool.submit(client.post, f"/admin/videos/{OLD}/delete", headers={"X-CSRF-Token": token})
        try:
            time.sleep(0.05)
            assert not deleting.done()
        finally:
            release.set()
        assert uploading.result(timeout=5).status_code == 200
        assert deleting.result(timeout=5).status_code == 409
    assert client.get("/public/current").json()["version"] == OLD


def test_device_schema_limits_and_token(client):
    route = "/api/devices/pi-one/heartbeat"
    assert client.post(route, json={}).status_code == 401
    for item in [{}, [], {"cached_version": "bad", "reboot_supported": True},
                 {"cached_version": None, "reboot_supported": 1},
                 {"cached_version": None, "reboot_supported": True, "unexpected": 1}]:
        assert client.post(route, json=item, headers=TOKEN).status_code == 400
    assert client.post(route, content=b"x" * 8193, headers=TOKEN).status_code == 413
    assert beat(client, device="bad$id").status_code == 400
    assert beat(client, device="x" * 65).status_code == 400
    assert client.get("/api/devices/unknown/commands", headers=TOKEN).status_code == 404
    assert beat(client).status_code == 200
    assert client.get("/api/devices/pi-one/commands").status_code == 401
    assert client.post("/api/devices/pi-one/commands/" + "a" * 32 + "/ack", json={}).status_code == 401


def test_device_inventory_bounded(client):
    for n in range(100):
        assert beat(client, device=f"pi-{n}").status_code == 200
    assert beat(client, device="pi-extra").status_code == 409
    assert beat(client, device="pi-0").status_code == 200


def test_reboot_queue_ack_replay_and_expiry(client):
    token = sign_in(client)
    assert queue(client, token).status_code == 404
    beat(client, supported=False)
    assert queue(client, token).status_code == 409
    beat(client)
    command = queue(client, token).json()
    assert queue(client, token).json() == command
    commands = client.get("/api/devices/pi-one/commands", headers=TOKEN).json()["commands"]
    assert commands[0]["id"] == command["command_id"]
    assert commands[0]["type"] == "reboot"
    assert 295 < commands[0]["expires_at"] - time.time() <= 300
    beat(client, device="pi-two")
    ack = f'/api/devices/pi-one/commands/{command["command_id"]}/ack'
    assert client.post(ack.replace("pi-one", "pi-two"), json={}, headers=TOKEN).status_code == 409
    assert client.post(ack, json={"extra": 1}, headers=TOKEN).status_code == 400
    assert client.post(ack, json={}, headers=TOKEN).json() == {"ok": True}
    assert client.post(ack, json={}, headers=TOKEN).status_code == 409
    assert client.get("/api/devices/pi-one/commands", headers=TOKEN).json() == {"commands": []}
    expired = queue(client, token).json()["command_id"]
    with sqlite3.connect(client.data_dir / "devices.sqlite3") as db:
        db.execute("UPDATE commands SET expires_at=? WHERE id=?", (time.time() - 1, expired))
    assert client.post(f"/api/devices/pi-one/commands/{expired}/ack", json={}, headers=TOKEN).status_code == 409
    assert client.get("/api/devices/pi-one/commands", headers=TOKEN).json() == {"commands": []}
    with sqlite3.connect(client.data_dir / "devices.sqlite3") as db:
        db.execute("UPDATE devices SET last_seen=? WHERE id='pi-one'", (time.time() - 91,))
    assert queue(client, token).status_code == 409
    assert next(d for d in client.get("/admin/devices").json()["devices"] if d["id"] == "pi-one")["online"] is False


def test_persistence_across_app_restart(tmp_path):
    settings = server.Settings("admin", "secret-password", "s" * 40, "t" * 40, tmp_path, 1, False)
    with TestClient(server.create_app(settings)) as first:
        token = sign_in(first)
        beat(first, version=OLD)
        command_id = queue(first, token).json()["command_id"]
        old_instance = first.get("/admin/status").json()["instance"]
    with TestClient(server.create_app(settings)) as second:
        sign_in(second)
        assert second.get("/admin/status").json()["instance"] != old_instance
        assert second.get("/admin/devices").json()["devices"][0]["cached_version"] == OLD
        assert second.get("/api/devices/pi-one/commands", headers=TOKEN).json()["commands"][0]["id"] == command_id


def test_restart_explicit_opt_in_and_duplicate_guard(tmp_path, client):
    token = sign_in(client)
    assert client.post("/admin/restart", headers={"X-CSRF-Token": token}).status_code == 403
    settings = server.Settings("admin", "secret-password", "s" * 40, "t" * 40, tmp_path, 1, False, allow_restart=True)
    with patch.object(server, "terminate_server") as terminate, TestClient(server.create_app(settings)) as enabled:
        enabled.app.state.started_at -= 20
        token = sign_in(enabled)
        enabled.portal.call(enabled.app.state.upload_lock.acquire)
        assert enabled.post("/admin/restart", headers={"X-CSRF-Token": token}).status_code == 409
        enabled.portal.call(enabled.app.state.upload_lock.release)
        response = enabled.post("/admin/restart", headers={"X-CSRF-Token": token})
        instance = enabled.get("/admin/status").json()["instance"]
        assert response.json() == {"status": "restarting", "instance": instance}
        assert not terminate.called
        assert publish(enabled, token, b"video").status_code == 409
        assert enabled.post("/admin/restart", headers={"X-CSRF-Token": token}).status_code == 409
        enabled.portal.call(server.asyncio.sleep, 1.1)
        terminate.assert_called_once_with()


def test_restart_environment_validation(monkeypatch):
    monkeypatch.setenv("SIGNAGE_ALLOW_RESTART", "true")
    assert server.Settings.from_env().allow_restart
    monkeypatch.setenv("SIGNAGE_ALLOW_RESTART", "not-bool")
    with pytest.raises(RuntimeError, match="SIGNAGE_ALLOW_RESTART"):
        server.Settings.from_env()
