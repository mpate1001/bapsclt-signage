#!/usr/bin/env python3
"""Opt-in offline Wi-Fi recovery. Run only the root-owned installed copy."""
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import threading
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

ADDRESS = '10.42.0.1'
PORT = 8091
HOTSPOT_ID = 'bapsclt-signage-setup'
HOTSPOT_UUID = '72c25775-673a-4b48-bf74-511894293f48'
CANDIDATE_PREFIX = 'bapsclt-signage-candidate-'


class NetworkError(Exception):
    pass


def escape(value):
    # GLib keyfile string escaping preserves leading/trailing spaces and backslashes.
    return value.replace('\\', '\\\\').replace(' ', '\\s').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')


def owned_profile(path, ident, name):
    return path.is_file() and not path.is_symlink() and path.stat().st_uid == 0 and not path.stat().st_mode & 0o077 and f'uuid={ident}\n' in path.read_text() and f'id={name}\n' in path.read_text()


def validate(ssid, password):
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode('utf-8')) <= 32 or any(ord(c) < 32 or ord(c) == 127 for c in ssid):
        raise ValueError('Enter a Wi-Fi name of 1 to 32 bytes without control characters.')
    if not isinstance(password, str) or (password and not ((8 <= len(password) <= 63 and all(32 <= ord(c) <= 126 for c in password)) or re.fullmatch(r'[0-9a-fA-F]{64}', password))):
        raise ValueError('Use an 8 to 63 character Wi-Fi password, or leave blank for an open network.')


class NetworkManager:
    def __init__(self, interface, password, directory=Path('/etc/NetworkManager/system-connections')):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,15}', interface):
            raise ValueError('Invalid Wi-Fi interface.')
        if password == 'replace-with-a-private-password' or not 12 <= len(password) <= 63 or not all(32 <= ord(c) <= 126 for c in password):
            raise ValueError('Set WIFI_HOTSPOT_PASSWORD to 12 to 63 printable ASCII characters.')
        self.interface, self.password, self.directory = interface, password, Path(directory)
        self.retry_offset = 0

    def run(self, *args, wait=10):
        try:
            result = subprocess.run(['/usr/bin/nmcli', '--wait', str(wait), *args], capture_output=True, text=True, timeout=wait + 5, env={**{key: value for key, value in os.environ.items() if not key.startswith('WIFI_')}, 'LC_ALL': 'C'})
        except (OSError, subprocess.TimeoutExpired):
            raise NetworkError('NetworkManager did not respond.') from None
        if result.returncode:
            # nmcli output can contain network names or secrets; never return it.
            raise NetworkError('NetworkManager could not complete the request.')
        return result.stdout.strip()

    def devices(self):
        names = self.run('-t', '--escape', 'no', '-f', 'DEVICE', 'device', 'status').splitlines()
        result = []
        for name in names:
            data = {}
            for line in self.run('-t', '--escape', 'no', '-f', 'GENERAL.TYPE,GENERAL.STATE,GENERAL.NM-MANAGED,GENERAL.CON-UUID,IP4.ADDRESS,IP6.ADDRESS', 'device', 'show', name).splitlines():
                key, _, value = line.partition(':')
                data.setdefault(key, []).append(value)
            state = data.get('GENERAL.STATE', [''])[0].split(' ', 1)[0]
            addresses = [address for key, values in data.items() if key.startswith(('IP4.ADDRESS[', 'IP6.ADDRESS[')) for address in values]
            valid_ip = False
            for address in addresses:
                try:
                    ip = ipaddress.ip_interface(address).ip
                    valid_ip |= not (ip.is_loopback or ip.is_link_local or ip.is_unspecified)
                except ValueError:
                    pass
            kind = data.get('GENERAL.TYPE', [''])[0] if data.get('GENERAL.NM-MANAGED', ['no'])[0] == 'yes' else 'unmanaged'
            result.append((name, kind, state == '100' and valid_ip, data.get('GENERAL.CON-UUID', [''])[0]))
        return result

    def snapshot(self):
        devices = self.devices()
        connected = any(kind in ('wifi', 'ethernet') and active and ident != HOTSPOT_UUID for _, kind, active, ident in devices)
        hotspot = any(name == self.interface and active and ident == HOTSPOT_UUID for name, _, active, ident in devices)
        # Unsupported, unmanaged or externally configured adapters fail closed.
        if not any(name == self.interface and kind == 'wifi' for name, kind, _, _ in devices):
            raise NetworkError('Configured Wi-Fi adapter is unavailable.')
        return connected, hotspot

    def profiles(self):
        lines = self.run('-t', '--escape', 'no', '-f', 'UUID,TYPE', 'connection', 'show').splitlines()
        return [line.partition(':') for line in lines]

    def write(self, ident, name, ssid, password, hotspot=False):
        path = self.directory / (('bapsclt-signage-setup' if hotspot else CANDIDATE_PREFIX + ident) + '.nmconnection')
        text = f'[connection]\nid={escape(name)}\nuuid={ident}\ntype=wifi\ninterface-name={self.interface}\nautoconnect=false\n\n[wifi]\nmode={"ap" if hotspot else "infrastructure"}\nssid={";".join(str(byte) for byte in ssid.encode("utf-8"))};\n'
        if password:
            text += f'\n[wifi-security]\nkey-mgmt=wpa-psk\npsk={escape(password)}\n'
            if hotspot:
                text += 'proto=rsn;\n'
        text += '\n[ipv4]\nmethod=' + ('shared\naddress1=10.42.0.1/24\nnever-default=true' if hotspot else 'auto') + '\n\n[ipv6]\nmethod=' + ('disabled' if hotspot else 'auto') + '\n'
        fd, temporary = tempfile.mkstemp(prefix='.bapsclt-', suffix='.tmp', dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
            # Link publishes a complete file atomically and refuses existing paths.
            os.link(temporary, path)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(temporary).unlink(missing_ok=True)
        try:
            self.run('connection', 'load', str(path))
        except NetworkError:
            path.unlink()
            raise
        return path

    def recover_candidates(self):
        loaded = {ident for ident, _, _ in self.profiles()}
        active = {ident for name, kind, connected, ident in self.devices() if name == self.interface and kind == 'wifi' and connected}
        for path in self.directory.glob(CANDIDATE_PREFIX + '*.nmconnection'):
            ident = path.name[len(CANDIDATE_PREFIX):-len('.nmconnection')]
            if not re.fullmatch(r'[0-9a-f-]{36}', ident) or not owned_profile(path, ident, CANDIDATE_PREFIX + ident):
                continue
            if ident not in loaded:
                # A saved successful profile may also be temporarily unloaded.
                self.run('connection', 'load', str(path))
            if self.run('-g', 'connection.autoconnect', 'connection', 'show', ident) == 'yes':
                continue
            if ident in active:
                self.run('connection', 'modify', 'uuid', ident, 'connection.autoconnect', 'yes')
            else:
                self.run('connection', 'delete', 'uuid', ident)
                path.unlink(missing_ok=True)

    def ensure_hotspot(self):
        if self.snapshot()[0]:
            return
        profiles = self.profiles()
        path = self.directory / 'bapsclt-signage-setup.nmconnection'
        if any(ident == HOTSPOT_UUID for ident, _, _ in profiles):
            if not owned_profile(path, HOTSPOT_UUID, HOTSPOT_ID):
                raise NetworkError('Setup profile ownership could not be verified.')
            if f'psk={escape(self.password)}\n' not in path.read_text():
                self.run('connection', 'delete', 'uuid', HOTSPOT_UUID)
                path.unlink(missing_ok=True)
                self.write(HOTSPOT_UUID, HOTSPOT_ID, 'BAPS-Signage-Setup', self.password, True)
        else:
            # IDs may contain colons, so query each ID independently.
            if any(self.run('-g', 'connection.id', 'connection', 'show', ident) == HOTSPOT_ID for ident, _, _ in profiles):
                raise NetworkError('Setup profile name is already in use.')
            if path.exists() or path.is_symlink():
                if not owned_profile(path, HOTSPOT_UUID, HOTSPOT_ID):
                    raise NetworkError('Setup profile ownership could not be verified.')
                # Recover a crash between writing the private profile and loading it.
                self.run('connection', 'load', str(path))
                if f'psk={escape(self.password)}\n' not in path.read_text():
                    self.run('connection', 'delete', 'uuid', HOTSPOT_UUID)
                    path.unlink(missing_ok=True)
                    self.write(HOTSPOT_UUID, HOTSPOT_ID, 'BAPS-Signage-Setup', self.password, True)
            else:
                self.write(HOTSPOT_UUID, HOTSPOT_ID, 'BAPS-Signage-Setup', self.password, True)
        if not self.snapshot()[0]:
            self.run('connection', 'up', 'uuid', HOTSPOT_UUID, 'ifname', self.interface, wait=30)

    def stop_hotspot(self):
        if self.snapshot()[1]:
            self.run('connection', 'down', 'uuid', HOTSPOT_UUID)

    def connect(self, ssid, password):
        validate(ssid, password)
        if self.snapshot()[0]:
            return
        ident = str(uuid.uuid4())
        path = self.write(ident, CANDIDATE_PREFIX + ident, ssid, password)
        try:
            self.run('connection', 'up', 'uuid', ident, 'ifname', self.interface, wait=30)
            if not any(name == self.interface and kind == 'wifi' and active and current == ident for name, kind, active, current in self.devices()):
                raise NetworkError('No network address was assigned.')
            self.run('connection', 'modify', 'uuid', ident, 'connection.autoconnect', 'yes')
        except NetworkError:
            try:
                self.run('connection', 'delete', 'uuid', ident)
            except NetworkError:
                pass
            path.unlink(missing_ok=True)
            raise

    def retry_saved(self):
        candidates = []
        for ident, _, kind in self.profiles():
            if kind == '802-11-wireless' and ident != HOTSPOT_UUID and self.run('-g', 'connection.autoconnect', 'connection', 'show', ident) == 'yes':
                candidates.append(ident)
        if candidates:
            offset = self.retry_offset % len(candidates)
            candidates = (candidates[offset:] + candidates[:offset])[:3]
            self.retry_offset += len(candidates)
        for ident in candidates:
            if self.snapshot()[0]:
                return
            try:
                self.run('connection', 'up', 'uuid', ident, 'ifname', self.interface, wait=30)
                if self.snapshot()[0]:
                    return
            except NetworkError:
                pass
        raise NetworkError('Saved Wi-Fi networks were not available.')


class Controller:
    def __init__(self, nm, delay=120):
        self.nm, self.delay = nm, delay
        self.offline_since = None
        self.mode, self.message = 'waiting', 'Waiting for a saved network.'
        self.lock = threading.Lock()
        self.busy = False
        self.worker = None
        self.stopped = threading.Event()

    def tick(self, now):
        if self.busy:
            return
        connected, hotspot = self.nm.snapshot()
        if connected:
            if hotspot:
                self.nm.stop_hotspot()
            self.mode, self.message = 'connected', 'Connected to a network.'
            self.offline_since = None
        elif hotspot:
            self.mode = 'setup'
        else:
            if self.offline_since is None:
                self.offline_since = now
            if now - self.offline_since >= self.delay:
                self.nm.ensure_hotspot()
                self.mode, self.message = 'setup', 'Connect this display to Wi-Fi.'

    def submit(self, ssid=None, password=None):
        with self.lock:
            if self.busy or self.mode != 'setup':
                return False
            self.busy = True
            self.mode, self.message = 'connecting', 'Trying the network. Reconnect to the setup hotspot if it returns.'
        def attempt():
            try:
                if self.stopped.wait(3):
                    return
                self.nm.stop_hotspot()
                if ssid is None:
                    self.nm.retry_saved()
                else:
                    self.nm.connect(ssid, password)
                self.mode, self.message = 'connected', 'Connected to a network.'
            except NetworkError:
                self.message = 'Could not connect. Check the Wi-Fi name and password and try again.'
                try:
                    self.nm.ensure_hotspot()
                    self.mode = 'setup'
                except NetworkError:
                    self.mode = 'waiting'
                    self.offline_since = None
            finally:
                self.busy = False
        self.worker = threading.Thread(target=attempt)
        self.worker.start()
        return True


def handler(controller, template, token):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            pass

        def allowed(self):
            try:
                return ipaddress.ip_address(self.client_address[0]) in ipaddress.ip_network('10.42.0.0/24') and self.headers.get('Host') == f'{ADDRESS}:{PORT}'
            except ValueError:
                return False

        def reply(self, code, value, html=False):
            data = value.encode() if html else json.dumps(value).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.allowed():
                return self.reply(403, {'error': 'Unavailable'})
            if self.path == '/':
                return self.reply(200, template.replace('__CSRF_TOKEN__', token), True)
            if self.path == '/status':
                return self.reply(200, {'mode': controller.mode, 'message': controller.message})
            self.reply(404, {'error': 'Unavailable'})

        def do_POST(self):
            if not self.allowed() or self.headers.get('Origin') != f'http://{ADDRESS}:{PORT}':
                return self.reply(403, {'error': 'Unavailable'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if self.headers.get('Transfer-Encoding') or not 0 < size <= 2048 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError('Invalid request.')
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict) or not isinstance(body.get('csrf_token'), str) or not secrets.compare_digest(body['csrf_token'], token):
                    return self.reply(403, {'error': 'Reload the setup page.'})
                if self.path == '/connect':
                    validate(body.get('ssid'), body.get('password'))
                    accepted = controller.submit(body['ssid'], body['password'])
                elif self.path == '/retry':
                    accepted = controller.submit()
                else:
                    return self.reply(404, {'error': 'Unavailable'})
            except (ValueError, UnicodeError, TimeoutError):
                return self.reply(400, {'error': 'Check the Wi-Fi name and password and try again.'})
            self.reply(202 if accepted else 409, {'message': controller.message})
    return Handler


def main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    nm = NetworkManager(os.environ.get('WIFI_INTERFACE', 'wlan0'), os.environ.get('WIFI_HOTSPOT_PASSWORD', ''))
    delay = int(os.environ.get('WIFI_FALLBACK_SECONDS', '120'))
    if not 30 <= delay <= 3600:
        raise ValueError('WIFI_FALLBACK_SECONDS must be 30 to 3600.')
    template = Path(__file__).with_name('wifi-setup.html').read_text()
    controller = Controller(nm, delay)
    token = secrets.token_hex(32)
    server = None
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    last_state = None
    try:
        nm.recover_candidates()
        while not stopped.is_set():
            try:
                controller.tick(time.monotonic())
                hotspot = nm.snapshot()[1]
                if controller.mode != last_state:
                    logging.info('Wi-Fi recovery mode: %s', controller.mode)
                    last_state = controller.mode
                if hotspot and server is None:
                    server = HTTPServer((ADDRESS, PORT), handler(controller, template, token))
                    server.timeout = 1
                if not hotspot and server:
                    server.server_close()
                    server = None
                if server:
                    server.handle_request()
                else:
                    stopped.wait(2)
            except (NetworkError, OSError):
                # Fail closed when NetworkManager is unhealthy; retry without takeover.
                if last_state != 'unavailable':
                    logging.warning('Wi-Fi recovery temporarily unavailable; checking again.')
                    last_state = 'unavailable'
                stopped.wait(5)
    finally:
        controller.stopped.set()
        if controller.worker:
            controller.worker.join()
        if server:
            server.server_close()
        try:
            nm.stop_hotspot()
        except NetworkError:
            pass


if __name__ == '__main__':
    main()
