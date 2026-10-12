#!/usr/bin/env python3
"""Verified offline cache and loopback video server. Python standard library only."""
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

LOG = logging.getLogger('signage')
VERSION = re.compile(r'[0-9a-f]{64}\Z')

class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        before, after = urlsplit(request.full_url), urlsplit(new_url)
        if (before.scheme, before.netloc) != (after.scheme, after.netloc):
            raise ValueError('Cross-origin redirect refused')
        return super().redirect_request(request, fp, code, message, headers, new_url)

class Cache:
    def __init__(self, directory, base_url, token, max_bytes=2_000_000_000):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.state_path = self.directory / 'state.json'

    def video_path(self, version):
        if not isinstance(version, str) or not VERSION.fullmatch(version):
            raise ValueError('Invalid version')
        return self.directory / (version + '.mp4')

    def state(self):
        with self.lock:
            try:
                state = json.loads(self.state_path.read_text())
                version = state.get('version')
                if VERSION.fullmatch(version or '') and self.video_path(version).is_file():
                    return {'version': version, 'video_url': '/video?version=' + version}
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            return {'version': None}

    def _open(self, url):
        headers = {'Authorization': 'Bearer ' + self.token, 'User-Agent': 'BAPSCLT-Signage/1.0'}
        return build_opener(SameOriginRedirect()).open(Request(url, headers=headers), timeout=30)

    def _publish(self, version):
        fd, temporary = tempfile.mkstemp(prefix='.state-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump({'version': version, 'video_url': '/video?version=' + version}, output)
                output.flush()
                os.fsync(output.fileno())
            with self.lock:
                os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def update(self):
        with self._open(self.base_url + '/api/current') as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError('Metadata exceeds limit')
        metadata = json.loads(raw)
        version = metadata.get('version')
        if version is None:
            return False
        destination = self.video_path(version)
        size = metadata.get('size')
        if metadata.get('sha256') != version or type(size) is not int or not 0 < size <= self.max_bytes:
            raise ValueError('Invalid metadata')
        parsed = urlsplit(metadata.get('video_url', ''))
        if parsed.scheme or parsed.netloc or parsed.path != '/api/video' or parsed.fragment:
            raise ValueError('Video URL must use configured server')
        if parse_qs(parsed.query) != {'version': [version]}:
            raise ValueError('URL version mismatch')
        if self.state().get('version') == version:
            return False
        if destination.exists():
            digest = hashlib.sha256()
            with destination.open('rb') as cached:
                for chunk in iter(lambda: cached.read(1024 * 1024), b''):
                    digest.update(chunk)
            if destination.stat().st_size == size and digest.hexdigest() == version:
                self._publish(version)
                return True
        fd, temporary = tempfile.mkstemp(prefix='.download-', dir=self.directory)
        try:
            digest, received = hashlib.sha256(), 0
            with os.fdopen(fd, 'wb') as output:
                with self._open(self.base_url + metadata['video_url']) as response:
                    for chunk in iter(lambda: response.read(1024 * 1024), b''):
                        received += len(chunk)
                        if received > size or received > self.max_bytes:
                            raise ValueError('Video exceeds declared size')
                        digest.update(chunk)
                        output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if received != size or digest.hexdigest() != version:
                raise ValueError('Video integrity check failed')
            os.replace(temporary, destination)
            self._publish(version)
            return True
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def handler_for(cache, index_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            LOG.debug('Local HTTP: ' + format, *args)
        def do_HEAD(self):
            self._serve(False)
        def do_GET(self):
            self._serve(True)
        def _bytes(self, body, content_type, send_body):
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            if send_body:
                self.wfile.write(body)
        def _serve(self, send_body):
            parsed = urlsplit(self.path)
            if parsed.path in ('/', '/index.html') and not parsed.query:
                self._bytes(Path(index_path).read_bytes(), 'text/html; charset=utf-8', send_body)
                return
            if parsed.path == '/state.json' and not parsed.query:
                self._bytes(json.dumps(cache.state()).encode(), 'application/json', send_body)
                return
            if parsed.path != '/video':
                self.send_error(404)
                return
            query = parse_qs(parsed.query)
            if set(query) != {'version'} or len(query['version']) != 1:
                self.send_error(404)
                return
            try:
                video = cache.video_path(query['version'][0]).open('rb')
            except (OSError, ValueError):
                self.send_error(404)
                return
            with video:
                size = os.fstat(video.fileno()).st_size
                start, end = 0, size - 1
                range_header = self.headers.get('Range')
                if range_header:
                    match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header)
                    valid = match is not None and size > 0
                    if valid:
                        first, last = match.groups()
                        if first:
                            start = int(first)
                            end = min(int(last), size - 1) if last else size - 1
                            valid = start < size and start <= end
                        elif last and int(last) > 0:
                            start = max(0, size - int(last))
                        else:
                            valid = False
                    if not valid:
                        self.send_response(416)
                        self.send_header('Content-Range', 'bytes */' + str(size))
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                        return
                self.send_response(206 if range_header else 200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Length', str(end - start + 1))
                self.send_header('Cache-Control', 'public, max-age=31536000, immutable')
                if range_header:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                self.end_headers()
                if send_body:
                    video.seek(start)
                    remaining = end - start + 1
                    try:
                        while remaining > 0:
                            chunk = video.read(min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
    return Handler


def poll(cache, interval, stop):
    while not stop.is_set():
        try:
            if cache.update():
                LOG.info('New video downloaded, verified and published')
        except Exception as error:
            # Log type only: remote errors/request objects may contain secrets.
            LOG.warning('Remote update failed (%s); keeping cached video', type(error).__name__)
        stop.wait(interval)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token = os.environ.get('SIGNAGE_API_TOKEN', '')
    if not token:
        raise SystemExit('Set SIGNAGE_API_TOKEN in the Pi environment file')
    base_url = os.environ.get('SIGNAGE_URL', 'https://bapsclt.datavault.digital')
    parsed = urlsplit(base_url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise SystemExit('SIGNAGE_URL must be an HTTP(S) origin')
    interval = float(os.environ.get('SIGNAGE_POLL_SECONDS', '30'))
    if interval < 1:
        raise SystemExit('SIGNAGE_POLL_SECONDS must be at least 1')
    cache = Cache(os.environ.get('SIGNAGE_DATA_DIR', '/home/admin/bapsclt-signage-data'), base_url, token,
                  int(os.environ.get('SIGNAGE_MAX_VIDEO_BYTES', '2000000000')))
    server = ThreadingHTTPServer(('127.0.0.1', 8090), handler_for(cache, Path(__file__).with_name('index.html')))
    server.daemon_threads = True
    stop = threading.Event()
    threading.Thread(target=poll, args=(cache, interval, stop), daemon=True).start()
    LOG.info('Local player available at http://127.0.0.1:8090/index.html')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()

if __name__ == '__main__':
    main()
