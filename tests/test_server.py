"""Server contracts, publication integrity, and access controls."""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

# The module intentionally refuses to start without credentials.
os.environ.setdefault("ADMIN_USERNAME", "test-admin")
os.environ.setdefault("ADMIN_PASSWORD", "test-password")
os.environ.setdefault("SESSION_SECRET", "s" * 40)
os.environ.setdefault("SIGNAGE_API_TOKEN", "t" * 40)
MODULE_PATH = Path(__file__).parents[1] / "server" / "main.py"
spec = importlib.util.spec_from_file_location("signage_server", MODULE_PATH)
server = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = server
spec.loader.exec_module(server)


@pytest.fixture
def client(tmp_path):
    settings = server.Settings("admin", "secret-password", "s" * 40, "t" * 40, tmp_path, 1, False)
    with TestClient(server.create_app(settings)) as value:
        value.data_dir = tmp_path
        yield value


def csrf(client):
    page = client.get("/admin")
    return re.search(r'const csrf = "([^"]+)";', page.text).group(1)


def sign_in(client):
    token = csrf(client)
    response = client.post("/admin/login", data={"username": "admin", "password": "secret-password", "csrf": token})
    assert response.status_code == 200
    return csrf(client)


def publish(client, token, body):
    return client.post("/admin/upload", content=body,
                       headers={"Content-Type": "video/mp4", "X-CSRF-Token": token})


def test_empty_views_and_api_auth(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/view"
    assert client.get("/view").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/public/current").json() == {"version": None}
    assert client.get("/public/video").status_code == 404
    assert client.get("/api/current").status_code == 401
    assert client.get("/api/current", headers={"Authorization": "Bearer " + "t" * 40}).json() == {"version": None}
    assert client.get("/api/video").status_code == 401


def test_required_configuration_is_fail_closed(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        server.Settings.from_env()


def test_login_csrf_and_upload_auth(client):
    token = csrf(client)
    assert client.post("/admin/login", data={"username": "admin", "password": "secret-password"}).status_code == 403
    assert publish(client, token, b"video").status_code == 401
    assert client.post("/admin/login", data={"username": "admin", "password": "wrong", "csrf": token}).status_code == 401
    authenticated_token = sign_in(client)
    assert authenticated_token != token
    assert publish(client, token, b"video").status_code == 403
    assert client.post("/admin/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/admin/logout", headers={"X-CSRF-Token": authenticated_token}).status_code == 200
    assert publish(client, authenticated_token, b"video").status_code == 401


def test_successful_publish_and_version_race(client):
    token = sign_in(client)
    old_body, new_body = b"first validated MP4", b"second validated MP4"
    with patch.object(server, "validate_video"):
        first = publish(client, token, old_body)
        second = publish(client, token, new_body)
    assert first.status_code == second.status_code == 200
    old_version = hashlib.sha256(old_body).hexdigest()
    new_version = hashlib.sha256(new_body).hexdigest()
    assert first.json() == {"version": old_version, "sha256": old_version, "size": len(old_body),
                            "video_url": "/public/video?version=" + old_version}
    assert client.get("/public/current").json()["version"] == new_version
    # A player that fetched old metadata must still receive precisely that version.
    assert client.get(first.json()["video_url"]).content == old_body
    assert client.get(second.json()["video_url"]).content == new_body
    response = client.get(first.json()["video_url"], headers={"Range": "bytes=0-3"})
    assert response.status_code == 206
    assert response.content == old_body[:4]
    assert "immutable" in response.headers["cache-control"]
    assert client.get("/public/video").headers["cache-control"] == "no-store"
    headers = {"Authorization": "Bearer " + "t" * 40}
    metadata = client.get("/api/current", headers=headers).json()
    assert metadata["video_url"] == "/api/video?version=" + new_version
    assert client.get(metadata["video_url"], headers=headers).content == new_body


def test_failed_publish_preserves_current_and_cleans_temp(client):
    token = sign_in(client)
    with patch.object(server, "validate_video"):
        original = publish(client, token, b"original video").json()
    with patch.object(server, "validate_video", side_effect=HTTPException(400, "bad mp4")):
        assert publish(client, token, b"corrupted file").status_code == 400
    assert client.get("/public/current").json() == original
    assert not list(client.data_dir.glob("videos/.upload-*"))
    assert not list(client.data_dir.glob(".current-*"))


def test_limits_empty_and_version_traversal(client):
    token = sign_in(client)
    assert publish(client, token, b"").status_code == 400
    assert publish(client, token, b"x" * (1024 * 1024 + 1)).status_code == 413
    assert client.get("/public/video?version=../../main.py").status_code == 400
    assert client.get("/public/video?version=" + "a" * 64).status_code == 404
    assert client.get("/public/current").json() == {"version": None}


def test_brute_force_is_bounded(client):
    token = csrf(client)
    data = {"username": "admin", "password": "wrong", "csrf": token}
    for _ in range(10):
        assert client.post("/admin/login", data=data).status_code == 401
    assert client.post("/admin/login", data=data).status_code == 429


def test_video_validator_checks_actual_codec(tmp_path):
    good = {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "10"},
            "streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p", "width": 1920, "height": 1080}]}
    result = subprocess.CompletedProcess([], 0, stdout=json.dumps(good).encode())
    with patch.object(server.subprocess, "run", return_value=result) as run:
        server.validate_video(tmp_path / "media.mp4")
        assert run.call_args.kwargs["timeout"] == 30
    good["streams"][0]["codec_name"] = "hevc"
    result.stdout = json.dumps(good).encode()
    with patch.object(server.subprocess, "run", return_value=result):
        with pytest.raises(HTTPException) as error:
            server.validate_video(tmp_path / "media.mp4")
        assert error.value.status_code == 400
    with patch.object(server.subprocess, "run", side_effect=FileNotFoundError):
        with pytest.raises(HTTPException) as error:
            server.validate_video(tmp_path / "media.mp4")
        assert error.value.status_code == 503


def test_corrupt_manifest_fails_without_serving_other_files(client):
    (client.data_dir / "current.json").write_text('{"version":"../main.py","size":1}')
    assert client.get("/public/current").status_code == 503
    assert client.get("/public/video").status_code == 503


def test_streamed_upload_enforces_limit_without_content_length(client):
    token = sign_in(client)
    chunks = (b"x" * (512 * 1024) for _ in range(3))
    response = client.post("/admin/upload", content=chunks,
                           headers={"Content-Type": "video/mp4", "X-CSRF-Token": token})
    assert response.status_code == 413
    assert client.get("/public/current").json() == {"version": None}
    assert not list(client.data_dir.glob("videos/.upload-*"))


def test_non_ascii_tokens_are_rejected_cleanly(client):
    token = sign_in(client)
    assert client.post("/admin/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    # Unicode login form values must not raise compare_digest's string limitation.
    assert client.post("/admin/login", data={"username": "é", "password": "é", "csrf": token}).status_code == 401


def test_validation_timeout_rejects(tmp_path):
    with patch.object(server.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffprobe", 30)):
        with pytest.raises(HTTPException) as error:
            server.validate_video(tmp_path / "media.mp4")
        assert error.value.status_code == 400


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="FFmpeg tools required")
def test_real_h264_video_roundtrip(client, tmp_path):
    media = tmp_path / "fixture.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=128x72:d=0.5",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(media)],
                   check=True, capture_output=True, timeout=30)
    token = sign_in(client)
    content = media.read_bytes()
    response = publish(client, token, content)
    assert response.status_code == 200, response.text
    metadata = response.json()
    assert metadata["sha256"] == hashlib.sha256(content).hexdigest()
    assert client.get(metadata["video_url"]).content == content
    # Actual ffprobe must reject plain text masquerading as MP4 and keep publication intact.
    assert publish(client, token, b"not actually an mp4").status_code == 400
    assert client.get("/public/current").json() == metadata
