"""Single-worker signage server: uvicorn server.main:app."""
import asyncio
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

VERSION = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class Settings:
    username: str
    password: str
    session_secret: str
    api_token: str
    data_dir: Path
    max_upload_mb: int = 1024
    cookie_secure: bool = True

    @classmethod
    def from_env(cls):
        names = ("ADMIN_USERNAME", "ADMIN_PASSWORD", "SESSION_SECRET", "SIGNAGE_API_TOKEN")
        values = {name: os.environ.get(name, "") for name in names}
        missing = [name for name, value in values.items() if not value.strip()]
        if missing:
            raise RuntimeError("Required configuration missing: " + ", ".join(missing))
        if len(values["SESSION_SECRET"]) < 32 or len(values["SIGNAGE_API_TOKEN"]) < 32:
            raise RuntimeError("SESSION_SECRET and SIGNAGE_API_TOKEN must each have at least 32 characters")
        limit = int(os.environ.get("MAX_UPLOAD_MB", "1024"))
        if limit < 1:
            raise RuntimeError("MAX_UPLOAD_MB must be positive")
        secure = os.environ.get("COOKIE_SECURE", "true").lower()
        if secure not in ("true", "false"):
            raise RuntimeError("COOKIE_SECURE must be true or false")
        return cls(values["ADMIN_USERNAME"], values["ADMIN_PASSWORD"], values["SESSION_SECRET"],
                   values["SIGNAGE_API_TOKEN"], Path(os.environ.get("DATA_DIR", "./data")),
                   limit, secure == "true")


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
    # Global limit avoids trusting reverse-proxy client IP headers. Single worker required.
    login_failures = []
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

    @asynccontextmanager
    async def lifespan(_):
        media_dir.mkdir(parents=True, exist_ok=True)
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
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
            raise HTTPException(403, "Session expired. Refresh the admin page and try again.")

    def check_admin(request):
        if not request.session.get("authenticated"):
            raise HTTPException(401, "Sign in to upload.")

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
    async def admin(request: Request):
        return templates.TemplateResponse(request=request, name="admin.html", context={
            "authenticated": bool(request.session.get("authenticated")), "csrf": csrf(request),
            "max_upload_mb": settings.max_upload_mb,
        })

    @app.post("/admin/login")
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
        if not (username_ok and password_ok):
            login_failures.append(now)
            raise HTTPException(401, "Incorrect username or password.")
        request.session.clear()
        request.session.update(authenticated=True, csrf=secrets.token_urlsafe(32))
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/logout")
    async def logout(request: Request):
        check_admin(request)
        check_csrf(request, request.headers.get("x-csrf-token"))
        request.session.clear()
        return JSONResponse({"ok": True})

    @app.post("/admin/upload")
    async def upload(request: Request):
        check_admin(request)
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
