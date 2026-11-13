"""Which page may ask thinksmart-kiosk.py for what (no WebKit needed: gi is stubbed).

  uv run --no-project --with pytest python -m pytest -q kiosk
"""
import importlib.util
import pathlib
import sys
import types

gi = types.ModuleType('gi')
gi.require_version = lambda *a: None
repo = types.ModuleType('gi.repository')
repo.GLib = repo.Gtk = repo.WebKit2 = None
sys.modules.setdefault('gi', gi)
sys.modules.setdefault('gi.repository', repo)

spec = importlib.util.spec_from_file_location('kiosk', pathlib.Path(__file__).with_name('thinksmart-kiosk.py'))
kiosk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kiosk)

CLIENT = 'http://127.0.0.1:8090/v2'


def test_welcome_page_may_do_everything():
    cmds = kiosk.page_commands('file://' + kiosk.WELCOME + '?ip=1', CLIENT)
    assert {'scan', 'connect', 'forget', 'wifi_status', 'update_install'} <= set(cmds)


def test_the_local_client_may_only_do_wifi():
    cmds = kiosk.page_commands('http://127.0.0.1:8090/v2?x#y', CLIENT)
    assert set(cmds) == {'scan', 'connect', 'forget', 'wifi_status'}
    assert set(kiosk.page_commands('http://localhost:8090/', 'http://localhost:8090/')) == set(cmds)


def test_other_pages_get_nothing():
    for uri, url in [
        ('http://127.0.0.1:9999/', CLIENT),                    # another local service
        ('http://192.168.1.5:8090/', 'http://192.168.1.5:8090/'),   # configured, but not this device
        ('https://example.com/', CLIENT),
        ('http://127.0.0.1:8090.evil.com/', CLIENT),
        ('file:///tmp/welcome.html', CLIENT),
        ('', CLIENT),
        ('http://127.0.0.1:8090/', ''),                       # no URL configured
    ]:
        assert kiosk.page_commands(uri, url) == {}, uri


def test_forget_reports_whether_nmcli_deleted_it(monkeypatch):
    # the pages say 已忘记 only on ok: a failed delete must not answer ok.
    # The new forget deletes every profile whose SSID matches (duplicates included).
    delete_calls = []

    def fake_nmcli(*args, timeout=10):
        if args[:3] == ('-t', '-f', 'UUID,TYPE'):
            return types.SimpleNamespace(stdout='u1:802-11-wireless\nu9:802-11-wireless\nu2:bridge\n', returncode=0)
        if args[0] == '-g':   # uuid/ssid pairs: u1 Home, u9 Other
            return types.SimpleNamespace(stdout='u1\nHome\nu9\nOther\n', returncode=0)
        if args[:2] == ('connection', 'delete'):
            delete_calls.append(args)
            return types.SimpleNamespace(stdout='', returncode=fake_nmcli.code)
        raise AssertionError(f'unexpected nmcli call: {args}')

    monkeypatch.setattr(kiosk, 'nmcli', fake_nmcli)
    for code, ok in ((0, True), (10, False)):
        fake_nmcli.code = code
        assert kiosk.wifi_forget({'ssid': 'Home'}, None) == {'ok': ok}
    assert delete_calls == [('connection', 'delete', 'uuid', 'u1')] * 2


def test_a_command_that_raises_still_answers():
    # the page waits for an answer: without one the Wi-Fi panel stays busy until a restart
    def hangs(msg, progress):
        raise kiosk.subprocess.TimeoutExpired(['nmcli'], 10)

    def breaks(msg, progress):
        raise RuntimeError('NetworkManager went away')
    assert kiosk.run_command(hangs, {'cmd': 'connect'}, None) == {'ok': False, 'reason': 'timeout'}
    r = kiosk.run_command(breaks, {'cmd': 'forget'}, None)
    assert r['ok'] is False and r['reason'] == 'failed'
    assert kiosk.run_command(lambda m, p: {'ok': True}, {'cmd': 'scan'}, None) == {'ok': True}
