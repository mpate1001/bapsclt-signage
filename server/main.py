"""Single-worker signage server: uvicorn server.main:app."""
import asyncio
import hashlib
import json
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

VERSION = re.compile(r"^[a-f0-9]{64}$")
DEVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def terminate_server():
    """Only this process; Docker restart policy handles the container."""
    os.kill(os.getpid(), signal.SIGTERM)


@dataclass(frozen=True)
class Settings:
    username: str
    password: str
    session_secret: str
    api_token: str
    data_dir: Path
    max_upload_mb: int = 2048
    cookie_secure: bool = True
    publisher_username: str = ""
    publisher_password: str = ""
    allow_restart: bool = False

    def __post_init__(self):
        if bool(self.publisher_username.strip()) != bool(self.publisher_password.strip()):
            raise RuntimeError("PUBLISHER_USERNAME and PUBLISHER_PASSWORD must both be set or both be blank")
        if self.publisher_username and self.publisher_username == self.username:
            raise RuntimeError("PUBLISHER_USERNAME must differ from ADMIN_USERNAME")

    @classmethod
    def from_env(cls):
        names = ("ADMIN_USERNAME", "ADMIN_PASSWORD", "SESSION_SECRET", "SIGNAGE_API_TOKEN")
        values = {name: os.environ.get(name, "") for name in names}
        missing = [name for name, value in values.items() if not value.strip()]
        if missing:
            raise RuntimeError("Required configuration missing: " + ", ".join(missing))
        if len(values["SESSION_SECRET"]) < 32 or len(values["SIGNAGE_API_TOKEN"]) < 32:
            raise RuntimeError("SESSION_SECRET and SIGNAGE_API_TOKEN must each have at least 32 characters")
        limit = int(os.environ.get("MAX_UPLOAD_MB", "2048"))
        if limit < 1:
            raise RuntimeError("MAX_UPLOAD_MB must be positive")
        secure = os.environ.get("COOKIE_SECURE", "true").lower()
        if secure not in ("true", "false"):
            raise RuntimeError("COOKIE_SECURE must be true or false")
        restart = os.environ.get("SIGNAGE_ALLOW_RESTART", "false").lower()
        if restart not in ("true", "false"):
            raise RuntimeError("SIGNAGE_ALLOW_RESTART must be true or false")
        return cls(values["ADMIN_USERNAME"], values["ADMIN_PASSWORD"], values["SESSION_SECRET"],
                   values["SIGNAGE_API_TOKEN"], Path(os.environ.get("DATA_DIR", "./data")),
                   limit, secure == "true", os.environ.get("PUBLISHER_USERNAME", ""),
                   os.environ.get("PUBLISHER_PASSWORD", ""), restart == "true")


def validate_video(path: Path):
    """Inspect the actual container and codec, never trust the filename."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True, timeout=30, check=True,
        )
        info = json.loads(result.stdout)
        streams = info.get("streams", [])
        video = [s for s in streams if s.get("codec_type") == "video"]
        format_info = info.get("format", {})
        valid = ("mp4" in format_info.get("format_name", "").split(",")
                 and float(format_info.get("duration", "0")) > 0
                 and len(video) == 1 and video[0].get("codec_name") == "h264"
                 and video[0].get("pix_fmt") == "yuv420p"
                 and video[0].get("width", 0) > 0 and video[0].get("height", 0) > 0)
        if not valid:
            raise ValueError("unsupported media")
    except FileNotFoundError as exc:
        raise HTTPException(503, "Video validation is unavailable; ffprobe must be installed.") from exc
    except (subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, "Use a valid MP4 with H.264 video and yuv420p pixel format.") from exc


def create_app(settings=None):
    settings = settings or Settings.from_env()
    media_dir = settings.data_dir / "videos"
    manifest_path = settings.data_dir / "current.json"
    upload_lock = asyncio.Lock()
    database_path = settings.data_dir / "devices.sqlite3"

    @contextmanager
    def database():
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def prune(db, now):
        db.execute("UPDATE commands SET status='expired' WHERE status='pending' AND expires_at<=?", (now,))
        db.execute("DELETE FROM commands WHERE status!='pending' AND expires_at<?", (now - 86400,))
        db.execute("DELETE FROM devices WHERE last_seen<? AND id NOT IN (SELECT device_id FROM commands WHERE status='pending')", (now - 30 * 86400,))

    def devices(db, now):
        return [{**dict(row), "online": now - row["last_seen"] <= 90,
                 "reboot_supported": bool(row["reboot_supported"])}
                for row in db.execute("SELECT * FROM devices ORDER BY id")]

    async def device_json(request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                raise HTTPException(413, "Device request too large.")
        try:
            item = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid device JSON.")
        if not isinstance(item, dict):
            raise HTTPException(400, "Expected a JSON object.")
        return item

    def check_device_id(device_id):
        if not DEVICE_ID.fullmatch(device_id):
            raise HTTPException(400, "Invalid device ID.")
    # Global limit avoids trusting reverse-proxy client IP headers. Single worker required.
    login_failures = []
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

    @asynccontextmanager
    async def lifespan(_):
        media_dir.mkdir(parents=True, exist_ok=True)
        with database() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, last_seen REAL NOT NULL,
                    cached_version TEXT, reboot_supported INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS commands (
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
                    expires_at REAL NOT NULL, status TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_command
                    ON commands(device_id) WHERE status='pending';
            """)
        initialize_chunks()
        cleanup_task = asyncio.create_task(cleanup_loop())
        app.state.started_at = time.monotonic()
        app.state.restart_task = None
        try:
            yield
        finally:
            cleanup_task.cancel()
            pending = list(chunk_jobs.values())
            for job in pending:
                job.cancel()
            await asyncio.gather(cleanup_task, *pending, return_exceptions=True)
            task = app.state.restart_task
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.instance_id = secrets.token_hex(16)
    app.state.upload_lock = upload_lock
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret,
                       session_cookie="signage_session", max_age=8 * 3600,
                       same_site="strict", https_only=settings.cookie_secure)

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if not request.url.path.endswith("/video"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def csrf(request):
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(32)
        return request.session["csrf"]

    def check_csrf(request, value):
        expected = request.session.get("csrf", "")
        if not expected or not isinstance(value, str) or not secrets.compare_digest(expected.encode(), value.encode()):
            raise HTTPException(403, "Session expired. Refresh the sign-in page and try again.")

    def session_role(request):
        role, username = request.session.get("role"), request.session.get("username")
        if role == "admin" and username == settings.username:
            return role
        if (role == "publisher" and settings.publisher_username.strip()
                and settings.publisher_password.strip() and username == settings.publisher_username):
            return role
        return None

    def check_portal(request):
        role = session_role(request)
        if role is None:
            raise HTTPException(401, "Sign in to upload.")
        if request.url.path.startswith("/admin") and role != "admin":
            raise HTTPException(403, "This account can only use the upload portal.")

    def check_token(request):
        value = request.headers.get("authorization", "")
        if not secrets.compare_digest(value.encode(), ("Bearer " + settings.api_token).encode()):
            raise HTTPException(401, "Invalid API token", headers={"WWW-Authenticate": "Bearer"})

    def current(public=False):
        if not manifest_path.exists():
            return {"version": None}
        try:
            item = json.loads(manifest_path.read_text())
            version = item["version"]
            if not isinstance(version, str) or not VERSION.fullmatch(version):
                raise ValueError("bad version")
            if not (media_dir / (version + ".mp4")).is_file():
                raise ValueError("missing media")
            return {"version": version, "sha256": version, "size": item["size"],
                    "video_url": ("/public/video" if public else "/api/video") + "?version=" + version}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(503, "Published media is unavailable.") from exc

    def video_response(version, public=False):
        explicit_version = version is not None
        if version is None:
            version = current()["version"]
        if version is None:
            raise HTTPException(404, "No video has been published.")
        if not VERSION.fullmatch(version):
            raise HTTPException(400, "Invalid video version.")
        path = media_dir / (version + ".mp4")
        if not path.is_file():
            raise HTTPException(404, "Video version not found.")
        return FileResponse(path, media_type="video/mp4", headers={
            "Cache-Control": ("public, max-age=31536000, immutable" if public else "private, max-age=31536000, immutable") if explicit_version else "no-store",
            "ETag": '"' + version + '"',
        })

    @app.get("/")
    async def home():
        return RedirectResponse("/view", status_code=302)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/view")
    async def view(request: Request):
        return templates.TemplateResponse(request=request, name="view.html", context={})

    @app.get("/admin")
    @app.get("/upload")
    async def admin(request: Request):
        portal_path = "/admin" if request.url.path == "/admin" else "/upload"
        role = session_role(request)
        if portal_path == "/admin" and role == "publisher":
            raise HTTPException(403, "This account can only use the upload portal.")
        return templates.TemplateResponse(request=request, name="admin.html", context={
            "authenticated": role is not None, "csrf": csrf(request),
            "max_upload_mb": settings.max_upload_mb, "portal_path": portal_path,
            "admin_controls": portal_path == "/admin" and role == "admin",
            "allow_restart": settings.allow_restart,
            "portal_title": "Signage admin" if portal_path == "/admin" else "Upload portal",
        })

    @app.post("/admin/login")
    @app.post("/upload/login")
    async def login(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                raise HTTPException(413, "Login request too large.")
        values = parse_qs(body.decode("utf-8", errors="replace"))
        check_csrf(request, values.get("csrf", [""])[0])
        now = time.monotonic()
        login_failures[:] = [stamp for stamp in login_failures if stamp > now - 60]
        if len(login_failures) >= 10:
            raise HTTPException(429, "Too many attempts. Wait one minute and try again.")
        username = values.get("username", [""])[0]
        password = values.get("password", [""])[0]
        username_ok = secrets.compare_digest(username.encode(), settings.username.encode())
        password_ok = secrets.compare_digest(password.encode(), settings.password.encode())
        role = "admin" if username_ok and password_ok else None
        portal_path = "/admin" if request.url.path == "/admin/login" else "/upload"
        if portal_path == "/upload" and settings.publisher_username.strip() and settings.publisher_password.strip():
            publisher_username_ok = secrets.compare_digest(username.encode(), settings.publisher_username.encode())
            publisher_password_ok = secrets.compare_digest(password.encode(), settings.publisher_password.encode())
            if publisher_username_ok and publisher_password_ok:
                role = "publisher"
        if role is None:
            login_failures.append(now)
            raise HTTPException(401, "Incorrect username or password.")
        request.session.clear()
        request.session.update(role=role, username=username, csrf=secrets.token_urlsafe(32))
        return RedirectResponse(portal_path, status_code=303)

    @app.post("/admin/logout")
    @app.post("/upload/logout")
    async def logout(request: Request):
        check_portal(request)
        check_csrf(request, request.headers.get("x-csrf-token"))
        request.session.clear()
        return JSONResponse({"ok": True})

    @app.post("/admin/upload")
    @app.post("/upload/upload")
    async def upload(request: Request):
        check_portal(request)
        check_csrf(request, request.headers.get("x-csrf-token"))
        if request.headers.get("content-type", "").split(";")[0] not in ("video/mp4", "application/octet-stream"):
            raise HTTPException(415, "Select an MP4 file.")
        max_bytes = settings.max_upload_mb * 1024 * 1024
        try:
            declared_size = int(request.headers.get("content-length", "0"))
        except ValueError:
            raise HTTPException(400, "Invalid content length.")
        if declared_size > max_bytes:
            raise HTTPException(413, "Video exceeds the upload limit.")
        if app.state.restart_task is not None:
            raise HTTPException(409, "Server restart is scheduled. Try again after it restarts.")
        if upload_lock.locked():
            raise HTTPException(409, "Another upload is publishing. Try again shortly.")
        async with upload_lock:
            fd, name = tempfile.mkstemp(prefix=".upload-", suffix=".mp4", dir=media_dir)
            temporary = Path(name)
            manifest_tmp = None
            try:
                digest = hashlib.sha256()
                size = 0
                with os.fdopen(fd, "wb") as output:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > max_bytes:
                            raise HTTPException(413, "Video exceeds the upload limit.")
                        output.write(chunk)
                        digest.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if size == 0:
                    raise HTTPException(400, "The selected file is empty.")
                await asyncio.to_thread(validate_video, temporary)
                version = digest.hexdigest()
                destination = media_dir / (version + ".mp4")
                if not destination.exists():
                    os.replace(temporary, destination)
                descriptor, manifest_name = tempfile.mkstemp(prefix=".current-", dir=settings.data_dir)
                manifest_tmp = Path(manifest_name)
                with os.fdopen(descriptor, "w") as output:
                    json.dump({"version": version, "size": size}, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(manifest_tmp, manifest_path)
                return current(public=True)
            finally:
                temporary.unlink(missing_ok=True)
                if manifest_tmp:
                    manifest_tmp.unlink(missing_ok=True)

    # Durable, bounded scratch uploads. Only generated identifiers become paths.
    chunks_dir = settings.data_dir / "uploads"
    chunk_locks = {}
    chunk_jobs = {}
    chunk_size = 8 * 1024 * 1024
    chunk_ttl = 86400

    def chunk_path(identifier, suffix=".part"):
        path = chunks_dir / (identifier + suffix)
        if chunks_dir.is_symlink() or path.is_symlink():
            raise HTTPException(409, "Upload storage is unavailable.")
        return path

    def chunk_state(row):
        state = {key: row[key] for key in ("id", "size", "chunk_size", "next_chunk", "received", "status", "error")}
        if row["status"] == "complete":
            state.update(version=row["version"], video_url="/public/video?version=" + row["version"])
        return state

    def chunk_row(identifier, owner=None):
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise HTTPException(404, "Upload not found.")
        with database() as db:
            row = db.execute("SELECT * FROM uploads WHERE id=?", (identifier,)).fetchone()
        if row is None or (owner is not None and (row["role"], row["username"]) != owner):
            raise HTTPException(404, "Upload not found.")
        return row

    def chunk_auth(request, mutation=False):
        check_portal(request)
        if mutation:
            check_csrf(request, request.headers.get("x-csrf-token"))
        return (session_role(request), request.session["username"])

    def restart_guard():
        if app.state.restart_task is not None:
            raise HTTPException(409, "Server restart is scheduled. Try again after it restarts.")

    def reconcile_chunk(row):
        path = chunk_path(row["id"])
        if not path.is_file() or path.stat().st_size < row["received"]:
            with database() as db:
                db.execute("UPDATE uploads SET status='failed', error=? WHERE id=?", ("Upload storage is incomplete. Select the file again.", row["id"]))
            raise HTTPException(409, "Upload storage is incomplete. Select the file again.")
        with open(path, "r+b") as stream:
            stream.truncate(row["received"])
            stream.flush()
            os.fsync(stream.fileno())
        return path

    def cleanup_chunks():
        if chunks_dir.is_symlink():
            return
        cutoff = time.time() - chunk_ttl
        with database() as db:
            rows = db.execute("SELECT id FROM uploads WHERE updated<?", (cutoff,)).fetchall()
            for row in rows:
                identifier = row["id"]
                lock = chunk_locks.get(identifier)
                if identifier in chunk_jobs or (lock and lock.locked()):
                    continue
                # Unlinking a symlink itself is safe; never follow its target.
                for suffix in (".part", ".chunk"):
                    (chunks_dir / (identifier + suffix)).unlink(missing_ok=True)
                db.execute("DELETE FROM upload_chunks WHERE upload_id=?", (identifier,))
                db.execute("DELETE FROM uploads WHERE id=?", (identifier,))
                chunk_locks.pop(identifier, None)
        # Crash leftovers have only generated names and are not published media.
        with database() as db:
            # Completed/failed records retain useful status, with a fixed history bound.
            old = db.execute("SELECT id FROM uploads WHERE status IN ('complete','failed') ORDER BY updated DESC LIMIT -1 OFFSET 128").fetchall()
            for row in old:
                identifier = row["id"]
                lock = chunk_locks.get(identifier)
                if identifier in chunk_jobs or (lock and lock.locked()):
                    continue
                for suffix in (".part", ".chunk"):
                    (chunks_dir / (identifier + suffix)).unlink(missing_ok=True)
                db.execute("DELETE FROM upload_chunks WHERE upload_id=?", (identifier,))
                db.execute("DELETE FROM uploads WHERE id=?", (identifier,))
                chunk_locks.pop(identifier, None)
        for path in chunks_dir.glob("*.chunk"):
            if re.fullmatch(r"[a-f0-9]{32}", path.stem) and path.stem not in chunk_jobs:
                lock = chunk_locks.get(path.stem)
                if not lock or not lock.locked():
                    path.unlink(missing_ok=True)

    def initialize_chunks():
        if chunks_dir.is_symlink():
            raise RuntimeError("Upload storage must not be a symlink")
        chunks_dir.mkdir(exist_ok=True)
        with database() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY, role TEXT NOT NULL, username TEXT NOT NULL,
                    filename TEXT NOT NULL, size INTEGER NOT NULL, chunk_size INTEGER NOT NULL,
                    next_chunk INTEGER NOT NULL DEFAULT 0, received INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'receiving', error TEXT, version TEXT,
                    updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS upload_chunks (
                    upload_id TEXT NOT NULL, chunk_index INTEGER NOT NULL, digest TEXT NOT NULL,
                    PRIMARY KEY(upload_id, chunk_index));
            """)
            rows = db.execute("SELECT * FROM uploads WHERE status IN ('receiving', 'finalizing')").fetchall()
        for row in rows:
            if row["status"] == "finalizing" and row["version"]:
                try:
                    published = current()["version"] == row["version"]
                except HTTPException:
                    published = False
                if published:
                    with database() as db:
                        db.execute("UPDATE uploads SET status='complete', error=NULL WHERE id=?", (row["id"],))
                    continue
            try:
                reconcile_chunk(row)
                with database() as db:
                    db.execute("UPDATE uploads SET status='receiving', error=NULL WHERE id=?", (row["id"],))
            except (OSError, HTTPException):
                with database() as db:
                    db.execute("UPDATE uploads SET status='failed', error=? WHERE id=?", ("Interrupted upload. Select the file again.", row["id"]))
        cleanup_chunks()

    async def cleanup_loop():
        while True:
            await asyncio.sleep(60)
            cleanup_chunks()

    async def small_json(request, empty=False):
        body = bytearray()
        async for data in request.stream():
            if len(body) + len(data) > 8192:
                raise HTTPException(413, "Upload metadata is too large.")
            body.extend(data)
        if not body and empty:
            return {}
        try:
            value = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid upload metadata.")
        if not isinstance(value, dict):
            raise HTTPException(400, "Expected an upload object.")
        return value

    @app.post("/admin/uploads")
    @app.post("/upload/uploads")
    async def create_chunk_upload(request: Request):
        owner = chunk_auth(request, True)
        restart_guard()
        item = await small_json(request)
        restart_guard()
        size, filename = item.get("size"), item.get("filename", "")
        if (set(item) - {"size", "filename"} or type(size) is not int or size < 1
                or not isinstance(filename, str) or len(filename) > 255):
            raise HTTPException(400, "Invalid upload size or filename.")
        if size > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, "Video exceeds the upload limit.")
        cleanup_chunks()
        identifier = secrets.token_hex(16)
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM uploads WHERE status IN ('receiving','finalizing')").fetchone()[0] >= 8:
                raise HTTPException(409, "Upload capacity is busy. Try again later.")
            if db.execute("SELECT COUNT(*) FROM uploads WHERE role=? AND username=? AND status IN ('receiving','finalizing')", owner).fetchone()[0] >= 2:
                raise HTTPException(409, "Finish or cancel your existing uploads first.")
            path = chunk_path(identifier)
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
            try:
                db.execute("INSERT INTO uploads(id,role,username,filename,size,chunk_size,updated) VALUES (?,?,?,?,?,?,?)", (identifier, *owner, filename, size, chunk_size, time.time()))
            except Exception:
                path.unlink(missing_ok=True)
                raise
        return JSONResponse(chunk_state(chunk_row(identifier)), status_code=201)

    @app.get("/admin/uploads/{identifier}")
    @app.get("/upload/uploads/{identifier}")
    async def upload_state(request: Request, identifier: str):
        return chunk_state(chunk_row(identifier, chunk_auth(request)))

    @app.post("/admin/uploads/{identifier}/chunks/{index}")
    @app.post("/upload/uploads/{identifier}/chunks/{index}")
    async def receive_chunk(request: Request, identifier: str, index: int):
        owner = chunk_auth(request, True)
        chunk_row(identifier, owner)
        restart_guard()
        async with chunk_locks.setdefault(identifier, asyncio.Lock()):
            restart_guard()
            row = chunk_row(identifier, owner)
            if row["status"] != "receiving":
                raise HTTPException(409, "Upload is no longer receiving data.")
            if index < 0 or index > row["next_chunk"] or index * row["chunk_size"] >= row["size"]:
                raise HTTPException(409, "Unexpected chunk index. Check upload status.")
            expected = min(row["chunk_size"], row["size"] - index * row["chunk_size"])
            if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
                raise HTTPException(415, "Expected binary chunk data.")
            length = request.headers.get("content-length")
            if length is not None:
                try:
                    declared = int(length)
                except ValueError:
                    raise HTTPException(400, "Invalid content length.")
                if declared != expected:
                    raise HTTPException(413 if declared > expected else 400, "Incorrect chunk length.")
            path = reconcile_chunk(row)
            temporary = chunk_path(identifier, ".chunk")
            digest, size = hashlib.sha256(), 0
            try:
                fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as output:
                    async for data in request.stream():
                        if size + len(data) > expected:
                            raise HTTPException(413, "Chunk exceeds its expected size.")
                        output.write(data)
                        digest.update(data)
                        size += len(data)
                if size != expected:
                    raise HTTPException(400, "Incomplete chunk. Retry this chunk.")
                restart_guard()
                checksum = digest.hexdigest()
                with database() as db:
                    prior = db.execute("SELECT digest FROM upload_chunks WHERE upload_id=? AND chunk_index=?", (identifier, index)).fetchone()
                if index < row["next_chunk"]:
                    if not prior or not secrets.compare_digest(prior["digest"], checksum):
                        raise HTTPException(409, "Retried chunk differs from the saved chunk.")
                    return chunk_state(row)
                with open(path, "ab") as output, open(temporary, "rb") as source:
                    while data := source.read(1024 * 1024):
                        output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                with database() as db:
                    db.execute("INSERT INTO upload_chunks VALUES (?,?,?)", (identifier, index, checksum))
                    db.execute("UPDATE uploads SET next_chunk=next_chunk+1, received=received+?, updated=? WHERE id=?", (size, time.time(), identifier))
                return chunk_state(chunk_row(identifier, owner))
            except OSError:
                raise HTTPException(507, "Upload storage is unavailable. Retry after freeing space.")
            finally:
                temporary.unlink(missing_ok=True)

    def inspect_chunk_file(path):
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            while data := source.read(1024 * 1024):
                digest.update(data)
        validate_video(path)
        return digest.hexdigest()

    async def finalize_chunk(identifier):
        try:
            async with upload_lock:
                row = chunk_row(identifier)
                path = reconcile_chunk(row)
                inspection = asyncio.create_task(asyncio.to_thread(inspect_chunk_file, path))
                try:
                    version = await asyncio.shield(inspection)
                except asyncio.CancelledError:
                    # The validator thread must finish before shutdown releases scratch state.
                    try:
                        await inspection
                    except Exception:
                        pass
                    raise
                with database() as db:
                    db.execute("UPDATE uploads SET version=?, updated=? WHERE id=?", (version, time.time(), identifier))
                destination = media_dir / (version + ".mp4")
                if destination.is_symlink():
                    raise HTTPException(409, "Media storage is unavailable.")
                if not destination.exists():
                    os.replace(path, destination)
                descriptor, name = tempfile.mkstemp(prefix=".current-", dir=settings.data_dir)
                temporary = Path(name)
                try:
                    with os.fdopen(descriptor, "w") as output:
                        json.dump({"version": version, "size": row["size"]}, output)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, manifest_path)
                    with database() as db:
                        db.execute("UPDATE uploads SET status='complete', error=NULL, updated=? WHERE id=?", (time.time(), identifier))
                    path.unlink(missing_ok=True)
                finally:
                    temporary.unlink(missing_ok=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not chunks_dir.is_symlink():
                (chunks_dir / (identifier + ".part")).unlink(missing_ok=True)
            message = exc.detail if isinstance(exc, HTTPException) and isinstance(exc.detail, str) else "Video could not be published. Try again."
            with database() as db:
                db.execute("UPDATE uploads SET status='failed', error=?, updated=? WHERE id=?", (message, time.time(), identifier))
        finally:
            chunk_jobs.pop(identifier, None)

    @app.post("/admin/uploads/{identifier}/complete")
    @app.post("/upload/uploads/{identifier}/complete")
    async def complete_chunk_upload(request: Request, identifier: str):
        owner = chunk_auth(request, True)
        chunk_row(identifier, owner)
        restart_guard()
        if await small_json(request, empty=True) != {}:
            raise HTTPException(400, "Expected an empty completion object.")
        async with chunk_locks.setdefault(identifier, asyncio.Lock()):
            restart_guard()
            row = chunk_row(identifier, owner)
            if row["status"] == "complete":
                return chunk_state(row)
            if row["status"] == "finalizing":
                return JSONResponse(chunk_state(row), status_code=202)
            if row["status"] != "receiving" or row["received"] != row["size"]:
                raise HTTPException(409, "Upload is incomplete or failed.")
            reconcile_chunk(row)
            with database() as db:
                db.execute("UPDATE uploads SET status='finalizing', updated=? WHERE id=?", (time.time(), identifier))
            chunk_jobs[identifier] = asyncio.create_task(finalize_chunk(identifier))
            return JSONResponse(chunk_state(chunk_row(identifier, owner)), status_code=202)

    @app.delete("/admin/uploads/{identifier}")
    @app.delete("/upload/uploads/{identifier}")
    async def cancel_chunk_upload(request: Request, identifier: str):
        owner = chunk_auth(request, True)
        chunk_row(identifier, owner)
        async with chunk_locks.setdefault(identifier, asyncio.Lock()):
            row = chunk_row(identifier, owner)
            if row["status"] not in ("receiving", "failed"):
                raise HTTPException(409, "Publishing or published uploads cannot be cancelled.")
            if chunks_dir.is_symlink():
                raise HTTPException(409, "Upload storage is unavailable.")
            for suffix in (".part", ".chunk"):
                (chunks_dir / (identifier + suffix)).unlink(missing_ok=True)
            with database() as db:
                db.execute("DELETE FROM upload_chunks WHERE upload_id=?", (identifier,))
                db.execute("DELETE FROM uploads WHERE id=?", (identifier,))
        chunk_locks.pop(identifier, None)
        return {"ok": True}

    def check_admin(request, mutation=False):
        check_portal(request)
        if session_role(request) != "admin":
            raise HTTPException(403, "Admin access required.")
        if mutation:
            check_csrf(request, request.headers.get("x-csrf-token"))

    @app.get("/admin/status")
    async def admin_status(request: Request):
        check_admin(request)
        return {"instance": app.state.instance_id}

    @app.get("/admin/devices")
    async def admin_devices(request: Request):
        check_admin(request)
        now = time.time()
        with database() as db:
            prune(db, now)
            return {"devices": devices(db, now)}

    @app.get("/admin/library")
    async def library(request: Request):
        check_admin(request)
        async with upload_lock:
            current_version = current()["version"]
            with database() as db:
                inventory = devices(db, time.time())
            videos = []
            for path in media_dir.glob("*.mp4"):
                if not VERSION.fullmatch(path.stem) or path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                videos.append({"version": path.stem, "size": stat.st_size,
                               "created_at": stat.st_mtime, "is_current": path.stem == current_version,
                               "in_use_by": [d["id"] for d in inventory if d["online"] and d["cached_version"] == path.stem]})
            return {"videos": sorted(videos, key=lambda v: v["created_at"], reverse=True)}

    @app.post("/admin/videos/{version}/delete")
    async def delete_video(request: Request, version: str):
        check_admin(request, mutation=True)
        if not VERSION.fullmatch(version):
            raise HTTPException(400, "Invalid video version.")
        async with upload_lock:
            if current()["version"] == version:
                raise HTTPException(409, "The published video cannot be deleted.")
            with database() as db:
                if any(d["online"] and d["cached_version"] == version for d in devices(db, time.time())):
                    raise HTTPException(409, "An online device is using this video.")
            path = media_dir / (version + ".mp4")
            if path.is_symlink() or not path.is_file():
                raise HTTPException(404, "Video version not found.")
            path.unlink()
            return {"ok": True}

    @app.post("/api/devices/{device_id}/heartbeat")
    async def heartbeat(request: Request, device_id: str):
        check_token(request)
        check_device_id(device_id)
        item = await device_json(request)
        version = item.get("cached_version")
        supported = item.get("reboot_supported")
        if (set(item) != {"cached_version", "reboot_supported"}
                or (version is not None and (not isinstance(version, str) or not VERSION.fullmatch(version)))
                or not isinstance(supported, bool)):
            raise HTTPException(400, "Invalid heartbeat fields.")
        # Shares publication/deletion lock so cached-video protection cannot race deletion.
        async with upload_lock:
            now = time.time()
            with database() as db:
                prune(db, now)
                if not db.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone():
                    if db.execute("SELECT COUNT(*) FROM devices").fetchone()[0] >= 100:
                        raise HTTPException(409, "Device inventory is full.")
                db.execute("INSERT INTO devices VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen, cached_version=excluded.cached_version, reboot_supported=excluded.reboot_supported",
                           (device_id, now, version, supported))
        return {"ok": True}

    @app.post("/admin/devices/{device_id}/reboot")
    async def reboot_device(request: Request, device_id: str):
        check_admin(request, mutation=True)
        check_device_id(device_id)
        now = time.time()
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            prune(db, now)
            device = db.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if not device:
                raise HTTPException(404, "Device not found.")
            if now - device["last_seen"] > 90 or not device["reboot_supported"]:
                raise HTTPException(409, "Device is offline or reboot is unavailable.")
            existing = db.execute("SELECT id FROM commands WHERE device_id=? AND status='pending'", (device_id,)).fetchone()
            if existing:
                return {"command_id": existing["id"], "status": "queued"}
            if db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] >= 1000:
                raise HTTPException(409, "Command inventory is full. Try again later.")
            command_id = secrets.token_hex(16)
            db.execute("INSERT INTO commands VALUES (?, ?, ?, 'pending')", (command_id, device_id, now + 300))
            return {"command_id": command_id, "status": "queued"}

    @app.get("/api/devices/{device_id}/commands")
    async def pending_commands(request: Request, device_id: str):
        check_token(request)
        check_device_id(device_id)
        with database() as db:
            prune(db, time.time())
            if not db.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone():
                raise HTTPException(404, "Device not found.")
            return {"commands": [{"id": row["id"], "type": "reboot", "expires_at": row["expires_at"]}
                                 for row in db.execute("SELECT id, expires_at FROM commands WHERE device_id=? AND status='pending'", (device_id,))]}

    @app.post("/api/devices/{device_id}/commands/{command_id}/ack")
    async def acknowledge(request: Request, device_id: str, command_id: str):
        check_token(request)
        check_device_id(device_id)
        if not re.fullmatch(r"[a-f0-9]{32}", command_id):
            raise HTTPException(400, "Invalid command ID.")
        if await device_json(request) != {}:
            raise HTTPException(400, "Expected an empty acknowledgment object.")
        with database() as db:
            updated = db.execute("UPDATE commands SET status='acknowledged' WHERE id=? AND device_id=? AND status='pending' AND expires_at>?",
                                 (command_id, device_id, time.time()))
            if updated.rowcount != 1:
                raise HTTPException(409, "Command is unavailable or already acknowledged.")
        return {"ok": True}

    @app.post("/admin/restart")
    async def restart(request: Request):
        check_admin(request, mutation=True)
        if not settings.allow_restart:
            raise HTTPException(403, "Server restart is disabled.")
        if upload_lock.locked() or chunk_jobs:
            raise HTTPException(409, "An upload is publishing. Try again shortly.")
        if app.state.restart_task is not None:
            raise HTTPException(409, "Server restart is already scheduled.")

        async def delayed_restart():
            # Docker activates restart policy after the container has run for ten seconds.
            await asyncio.sleep(max(1, 11 - (time.monotonic() - app.state.started_at)))
            terminate_server()

        app.state.restart_task = asyncio.create_task(delayed_restart())
        return {"status": "restarting", "instance": app.state.instance_id}

    @app.get("/api/current")
    async def api_current(request: Request):
        check_token(request)
        return current()

    @app.get("/api/video")
    async def api_video(request: Request, version: str | None = None):
        check_token(request)
        return video_response(version)

    @app.get("/public/current")
    async def public_current():
        return current(public=True)

    @app.get("/public/video")
    async def public_video(version: str | None = None):
        return video_response(version, public=True)

    return app


app = create_app()
