import importlib.util
from pathlib import Path
import subprocess
import threading
import pytest

spec = importlib.util.spec_from_file_location('wifi_setup', Path(__file__).parents[1] / 'pi/wifi-setup.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class FakeNM:
    def __init__(self):
        self.connected = False
        self.hotspot = False
        self.started = 0
    def snapshot(self):
        return self.connected, self.hotspot
    def ensure_hotspot(self):
        self.started += 1
        self.hotspot = True
    def stop_hotspot(self):
        self.hotspot = False
    def connect(self, *_):
        raise w.NetworkError('failure')
    def scan(self, cached=False):
        raise w.NetworkError()
    def retry_saved(self):
        raise w.NetworkError('failure')


def test_fallback_timer_and_ethernet_return():
    nm = FakeNM()
    c = w.Controller(nm)
    c.tick(0)
    c.tick(119)
    assert nm.started == 0
    c.tick(120)
    assert nm.hotspot
    nm.connected = True
    c.tick(121)
    assert not nm.hotspot and c.mode == 'connected'


def test_connected_network_without_internet_never_starts_hotspot():
    nm = FakeNM()
    nm.connected = True
    c = w.Controller(nm)
    c.tick(0)
    c.tick(99999)
    assert nm.started == 0


def test_nm_failure_does_not_take_over():
    nm = FakeNM()
    nm.snapshot = lambda: (_ for _ in ()).throw(w.NetworkError())
    c = w.Controller(nm)
    with pytest.raises(w.NetworkError):
        c.tick(99999)
    assert nm.started == 0


def test_bad_credentials_restore_hotspot():
    nm = FakeNM()
    nm.hotspot = True
    c = w.Controller(nm)
    c.mode = 'setup'
    class Immediate:
        def wait(self, _): return False
    c.stopped = Immediate()
    assert c.submit('new-network', 'private-secret')
    c.worker.join(2)
    assert not c.busy and nm.hotspot and c.mode == 'setup'
    assert 'private-secret' not in c.message


def test_shutdown_cancels_pending_attempt():
    nm = FakeNM()
    nm.hotspot = True
    c = w.Controller(nm)
    c.mode = 'setup'
    c.stopped.set()
    c.submit('network', 'private-secret')
    c.worker.join(2)
    assert nm.started == 0


@pytest.mark.parametrize('ssid,password', [('', 'abcdefgh'), ('x'*33, 'abcdefgh'), ('bad\nssid','abcdefgh'), ('ssid','short'), ('ssid','é'*12), ('ssid', None)])
def test_validation(ssid, password):
    with pytest.raises(ValueError):
        w.validate(ssid, password)


def test_glib_escape_and_no_password_in_argv(tmp_path):
    nm = w.NetworkManager('wlan0', 'hotspot-password', tmp_path)
    calls = []
    nm.run = lambda *args, **kwargs: calls.append(args) or ''
    path = nm.write('test-uuid', 'test-name', ' leading\\name ', ' secret\\pass ')
    text = path.read_text()
    assert 'ssid=' + ';'.join(str(b) for b in ' leading\\name '.encode()) + ';\n' in text
    assert 'psk=\\ssecret\\\\pass\\s\n' in text
    assert path.stat().st_mode & 0o777 == 0o600
    assert all(' secret\\pass ' not in str(call) for call in calls)
    with pytest.raises(FileExistsError):
        nm.write('test-uuid', 'test-name', 'ssid', 'abcdefgh')


def test_nm_timeout_is_sanitized(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired('SECRET', 10)
    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(w.NetworkError) as result:
        w.NetworkManager('wlan0', 'hotspot-password').run('connection', 'show')
    assert 'SECRET' not in str(result.value)


def test_candidate_requires_own_address_and_preserves_existing(tmp_path):
    nm = w.NetworkManager('wlan0', 'hotspot-password', tmp_path)
    calls = []
    nm.run = lambda *args, **kwargs: calls.append(args) or ''
    nm.snapshot = lambda: (False, False)
    nm.devices = lambda: [('eth0', 'ethernet', True, 'existing')]
    existing = tmp_path / 'existing.nmconnection'
    existing.write_text('unchanged')
    with pytest.raises(w.NetworkError):
        nm.connect('new', 'private-pass')
    assert existing.read_text() == 'unchanged'
    assert not list(tmp_path.glob(w.CANDIDATE_PREFIX + '*'))
    assert any(call[:2] == ('connection', 'delete') for call in calls)


def test_retry_is_bounded_and_rotates():
    nm = w.NetworkManager('wlan0', 'hotspot-password')
    nm.profiles = lambda: [(str(i), ':', '802-11-wireless') for i in range(5)]
    nm.snapshot = lambda: (False, False)
    attempts = []
    def run(*args, **kwargs):
        if args[0] == '-g': return 'yes'
        attempts.append(args[3])
        raise w.NetworkError()
    nm.run = run
    for _ in range(2):
        with pytest.raises(w.NetworkError): nm.retry_saved()
    assert attempts == ['0','1','2','3','4','0']


def test_device_snapshot_requires_managed_ip_and_excludes_hotspot():
    nm = w.NetworkManager('wlan0', 'hotspot-password')
    def run(*args, **kwargs):
        if args[-2:] == ('device', 'status'): return 'wlan0\neth0'
        name = args[-1]
        if name == 'wlan0':
            return f'GENERAL.TYPE:wifi\nGENERAL.STATE:100 (connected)\nGENERAL.NM-MANAGED:yes\nGENERAL.CON-UUID:{w.HOTSPOT_UUID}\nIP4.ADDRESS[1]:10.42.0.1/24'
        return 'GENERAL.TYPE:ethernet\nGENERAL.STATE:100 (connected)\nGENERAL.NM-MANAGED:yes\nGENERAL.CON-UUID:ethernet\nIP6.ADDRESS[1]:fe80::1/64\nIP6.ADDRESS[2]:2001:db8::1/64'
    nm.run = run
    assert nm.snapshot() == (True, True)


def test_placeholder_password_rejected():
    with pytest.raises(ValueError):
        w.NetworkManager('wlan0', 'replace-with-a-private-password')


def test_http_rejects_remote_host_origin_and_csrf():
    # A local test listener simulates an AP client without binding the actual AP IP.
    from http.client import HTTPConnection
    from http.server import HTTPServer
    nm = FakeNM()
    c = w.Controller(nm)
    c.mode = 'setup'
    class LocalAPClient:
        allowed = True
        def __contains__(self, address): return self.allowed
    # Test client must be in AP range in production; substitute only network membership.
    old_network = w.ipaddress.ip_network
    w.ipaddress.ip_network = lambda _: LocalAPClient()
    server = HTTPServer(('127.0.0.1', 0), w.handler(c, '__CSRF_TOKEN__', 'token'))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        def request(method, path, body=None, headers=None):
            conn = HTTPConnection('127.0.0.1', server.server_port, timeout=2)
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            status, data = response.status, response.read()
            conn.close()
            return status, data
        w.ipaddress.ip_network = old_network
        assert request('GET', '/', headers={'Host': '10.42.0.1:8091'})[0] == 403
        w.ipaddress.ip_network = lambda _: LocalAPClient()
        assert request('GET', '/', headers={'Host': 'evil.example'})[0] == 403
        assert request('GET', '/', headers={'Host': '10.42.0.1:8091'}) == (200, b'token')
        headers = {'Host':'10.42.0.1:8091','Content-Type':'application/json','Origin':'http://evil.example'}
        assert request('POST','/retry', '{"csrf_token":"token"}', headers)[0] == 403
        headers['Origin'] = 'http://10.42.0.1:8091'
        assert request('POST','/retry', '{"csrf_token":"wrong"}', headers)[0] == 403
        assert request('POST','/connect', '{"csrf_token":"token","ssid":"new","password":"short"}', headers)[0] == 400
        assert request('POST','/retry', 'x'*2049, headers)[0] == 400
        LocalAPClient.allowed = False
        assert request('GET', '/', headers={'Host': '10.42.0.1:8091'})[0] == 403
        LocalAPClient.allowed = True
        c.stopped.set()
        assert request('POST','/connect', '{"csrf_token":"token","ssid":"new","password":"private-password"}', headers)[0] == 202
        c.busy = True
        assert request('POST','/retry', '{"csrf_token":"token"}', headers)[0] == 409
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        w.ipaddress.ip_network = old_network


def test_unregistered_owned_candidate_recovered(tmp_path, monkeypatch):
    nm = w.NetworkManager('wlan0', 'hotspot-password', tmp_path)
    ident = 'c1d2ae20-e66d-43b4-91ec-42206ee79979'
    path = tmp_path / (w.CANDIDATE_PREFIX + ident + '.nmconnection')
    path.write_text(f'id={w.CANDIDATE_PREFIX}{ident}\nuuid={ident}\n')
    other = tmp_path / 'user-profile.nmconnection'
    other.write_text('preserve')
    monkeypatch.setattr(w, 'owned_profile', lambda path, ident, name: f'uuid={ident}\n' in path.read_text() and f'id={name}\n' in path.read_text())
    nm.profiles = lambda: []
    nm.devices = lambda: []
    calls = []
    nm.run = lambda *args, **kwargs: calls.append(args) or ('no' if args[0] == '-g' else '')
    nm.recover_candidates()
    assert calls[0] == ('connection', 'load', str(path))
    assert not path.exists() and other.read_text() == 'preserve'


def test_unloaded_owned_hotspot_recovers(tmp_path, monkeypatch):
    nm = w.NetworkManager('wlan0', 'hotspot-password', tmp_path)
    path = tmp_path / 'bapsclt-signage-setup.nmconnection'
    path.write_text(f'id={w.HOTSPOT_ID}\nuuid={w.HOTSPOT_UUID}\npsk=hotspot-password\n')
    monkeypatch.setattr(w, 'owned_profile', lambda path, ident, name: f'uuid={ident}\n' in path.read_text() and f'id={name}\n' in path.read_text())
    nm.profiles = lambda: []
    nm.snapshot = lambda: (False, False)
    calls = []
    nm.run = lambda *args, **kwargs: calls.append(args) or ''
    nm.ensure_hotspot()
    assert calls == [('connection', 'load', str(path)), ('connection', 'up', 'uuid', w.HOTSPOT_UUID, 'ifname', 'wlan0')]


def test_unloaded_hotspot_collision_preserved(tmp_path):
    nm = w.NetworkManager('wlan0', 'hotspot-password', tmp_path)
    path = tmp_path / 'bapsclt-signage-setup.nmconnection'
    path.write_text('user content')
    nm.profiles = lambda: []
    nm.snapshot = lambda: (False, False)
    with pytest.raises(w.NetworkError):
        nm.ensure_hotspot()
    assert path.read_text() == 'user content'


def test_write_failure_never_publishes_partial_profile(tmp_path, monkeypatch):
    nm = w.NetworkManager('wlan0', 'hotspot-password', tmp_path)
    monkeypatch.setattr(w.os, 'fsync', lambda _: (_ for _ in ()).throw(OSError('write failure')))
    with pytest.raises(OSError):
        nm.write('test-uuid', 'test-name', 'SSID', 'private-password')
    assert list(tmp_path.iterdir()) == []


def test_successful_unloaded_candidate_credentials_preserved(tmp_path, monkeypatch):
    nm = w.NetworkManager('wlan0', 'hotspot-password', tmp_path)
    ident = 'c1d2ae20-e66d-43b4-91ec-42206ee79979'
    path = tmp_path / (w.CANDIDATE_PREFIX + ident + '.nmconnection')
    original = f'id={w.CANDIDATE_PREFIX}{ident}\nuuid={ident}\nautoconnect=true\n'
    path.write_text(original)
    monkeypatch.setattr(w, 'owned_profile', lambda path, ident, name: f'uuid={ident}\n' in path.read_text() and f'id={name}\n' in path.read_text())
    nm.profiles = lambda: []
    nm.devices = lambda: []
    calls = []
    nm.run = lambda *args, **kwargs: calls.append(args) or ('yes' if args[0] == '-g' else '')
    nm.recover_candidates()
    assert path.read_text() == original
    assert calls[0] == ('connection', 'load', str(path))
    assert not any(call[:2] == ('connection', 'delete') for call in calls)


def test_network_scan_parser_preserves_names_and_classifies():
    rows = r''' leading\:name\\ :30:WPA2
 leading\:name\\ :90:WPA2
Open:55:--
Enterprise:80:WPA2 802.1X
Modern:90:WPA3
Old:70:WEP
BAPS-Signage-Setup:100:WPA2
:20:WPA2
65;66;:10:WPA2'''
    result = w.parse_networks(rows)
    by_name = {item['ssid']: item for item in result}
    assert by_name[' leading:name\\ ']['signal'] == 90
    assert by_name['Open']['security'] == 'open'
    assert by_name['Enterprise']['security'] == 'unsupported'
    assert not by_name['Modern']['supported'] and not by_name['Old']['supported']
    assert 'BAPS-Signage-Setup' not in by_name and '' not in by_name
    assert '65;66;' in by_name
    assert len(w.parse_networks('\n'.join(f'Network{i}:80:WPA2' for i in range(200)))) == 100


@pytest.mark.parametrize('second_fails', [False, True])
def test_scan_ap_pause_and_restore_serialization(second_fails):
    nm = FakeNM()
    nm.hotspot = True
    scans = []
    def scan(cached=False):
        scans.append(True)
        if len(scans) == 1 or second_fails:
            raise w.NetworkError()
        return [{'ssid':'Nearby','signal':90,'security':'personal','supported':True}]
    nm.scan = scan
    c = w.Controller(nm)
    c.mode = 'setup'
    c.networks = [{'ssid':'Previous'}]
    class Gate:
        def __init__(self): self.ready = threading.Event()
        def wait(self, _): self.ready.wait(2); return False
        def is_set(self): return False
    c.stopped = Gate()
    assert c.refresh(100)
    assert not c.submit('network','private-password') and not c.submit() and not c.refresh(101)
    c.stopped.ready.set()
    c.worker.join(3)
    assert nm.hotspot and nm.started == 1
    assert not c.busy and not c.scanning
    assert c.cached == second_fails
    assert c.networks[0]['ssid'] == ('Previous' if second_fails else 'Nearby')
    assert not c.refresh(105)


def test_scan_shutdown_and_outside_setup_do_not_scan():
    nm = FakeNM()
    c = w.Controller(nm)
    assert not c.refresh()
    c.mode = 'setup'
    c.stopped.set()
    assert c.refresh()
    c.worker.join(2)
    assert not c.busy and nm.started == 0


def test_existing_hotspot_uses_cached_scan_after_service_restart():
    nm = FakeNM()
    nm.hotspot = True
    calls = []
    nm.scan = lambda cached=False: calls.append(cached) or [{'ssid':'Saved result'}]
    c = w.Controller(nm)
    c.tick(0)
    c.tick(1)
    assert calls == [True] and c.cached and c.networks[0]['ssid'] == 'Saved result'


def test_scan_does_not_reactivate_hotspot_when_network_returns():
    class ArrivingNM(FakeNM):
        def scan(self, cached=False):
            if self.hotspot:
                raise w.NetworkError()
            self.connected = True
            return []
        def ensure_hotspot(self):
            if not self.connected: super().ensure_hotspot()
    nm = ArrivingNM()
    nm.hotspot = True
    c = w.Controller(nm)
    c.mode = 'setup'
    class Immediate:
        def wait(self, _): return False
        def is_set(self): return False
    c.stopped = Immediate()
    c.refresh()
    c.worker.join(2)
    assert nm.connected and not nm.hotspot and nm.started == 0


def test_scan_requests_completed_refresh_and_preserves_output():
    nm = w.NetworkManager('wlan0', 'hotspot-password')
    calls = []
    stamps = iter([100, 101])
    nm.last_scan = lambda: next(stamps)
    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return ' leading :90:WPA2\n'
    nm.run = run
    assert nm.scan()[0]['ssid'] == ' leading '
    assert calls[0][0] == ('device','wifi','rescan','ifname','wlan0')
    assert calls[1][0][-2:] == ('--rescan','yes')
    assert calls[1][1] == {'wait':15,'preserve':True}
    nm.scan(cached=True)
    assert calls[2][0][-2:] == ('--rescan','no')
    assert calls[3][0][-2:] == ('--rescan','no')


def test_scan_cached_exit_zero_is_not_fresh():
    nm = w.NetworkManager('wlan0', 'hotspot-password')
    nm.last_scan = lambda: 123
    nm.run = lambda *args, **kwargs: 'OldNetwork:80:WPA2\n'
    with pytest.raises(w.NetworkError):
        nm.scan()
    assert nm.scan(cached=True)[0]['ssid'] == 'OldNetwork'


def test_scan_rejected_request_does_not_read_stale_list():
    nm = w.NetworkManager('wlan0', 'hotspot-password')
    nm.last_scan = lambda: 123
    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        raise w.NetworkError()
    nm.run = run
    with pytest.raises(w.NetworkError):
        nm.scan()
    assert calls == [('device','wifi','rescan','ifname','wlan0')]


def test_scan_timestamp_status_and_bad_device_path(monkeypatch):
    nm = w.NetworkManager('wlan0', 'hotspot-password')
    nm.run = lambda *args, **kwargs: '/org/freedesktop/NetworkManager/Devices/2'
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, 'x 4321\n', '')
    monkeypatch.setattr(subprocess, 'run', run)
    assert nm.last_scan() == 4321
    assert calls[0][-2:] == ['org.freedesktop.NetworkManager.Device.Wireless', 'LastScan']
    nm.run = lambda *args, **kwargs: 'bad;path'
    with pytest.raises(w.NetworkError): nm.last_scan()
    assert len(calls) == 1


@pytest.mark.parametrize('result', ['malformed','failed','timeout'])
def test_scan_timestamp_failure_sanitized(monkeypatch, result):
    nm = w.NetworkManager('wlan0', 'hotspot-password')
    nm.run = lambda *args, **kwargs: '/org/freedesktop/NetworkManager/Devices/2'
    def run(args, **kwargs):
        if result == 'timeout':
            raise subprocess.TimeoutExpired('private-output', 5)
        return subprocess.CompletedProcess(args, 1 if result == 'failed' else 0, 'private-output', 'private-output')
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(w.NetworkError) as error:
        nm.last_scan()
    assert 'private-output' not in str(error.value)


def test_scan_rereads_cache_after_completed_timestamp():
    nm = w.NetworkManager('wlan0', 'hotspot-password')
    stamps = iter([100, 101])
    nm.last_scan = lambda: next(stamps)
    def run(*args, **kwargs):
        if args[-2:] == ('--rescan','yes'): return 'Old:80:WPA2\n'
        if args[-2:] == ('--rescan','no'): return 'New:80:WPA2\n'
        return ''
    nm.run = run
    assert nm.scan()[0]['ssid'] == 'New'
