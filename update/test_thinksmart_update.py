"""thinksmart-update clears an install whose process is gone (killed by systemd's timeout, a
crash, a power cut) before it handles a command, so the page doesn't show it as running forever;
releases are unsigned, so only https sources, well-formed manifests and matching sha256 get in.

  uv run --no-project --with pytest python -m pytest -q update
"""
import functools
import hashlib
import http.server
import importlib.machinery
import importlib.util
import io
import json
import sys
import tarfile
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name('thinksmart-update')


@pytest.fixture
def tu(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader(f'thinksmart_update_{tmp_path.name}', str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    state, opt = tmp_path / 'state', tmp_path / 'opt'
    m.CONF = tmp_path / 'none.conf'           # no SOURCES: check fails at once, no network
    m.STATE, m.STATUS, m.REQUESTS = state, state / 'status.json', state / 'requests'
    m.DOWNLOAD, m.PENDING, m.LOCK = state / 'download', state / 'pending.json', tmp_path / 'lock'
    m.OPT, m.CLIENT, m.ENVS = opt, opt / 'client', opt / 'envs'
    for d in (m.CLIENT, m.ENVS, state):
        d.mkdir(parents=True)
    monkeypatch.setattr(m.shutil, 'chown', lambda *a: None)    # group ubuntu: not on this machine
    m.restart_client = lambda version: None
    m.self_test = lambda version, need_hub, seconds: (True, '')

    def main(*args):
        monkeypatch.setattr(sys, 'argv', ['thinksmart-update', *args])
        return m.main()
    m.run_main = main
    return m


def job_left(m, state, version='1.2.0'):
    m.save_status({'current': '1.1.0', 'job': {'state': state, 'version': version, 'message': '',
                                                'progress': 0.4, 'time': 0}})


def install_dirs(m, *versions):
    for v in versions:
        (m.CLIENT / v).mkdir()


def test_check_clears_a_download_that_was_killed(tu):
    job_left(tu, 'downloading')
    (tu.CLIENT / '1.2.0.new').mkdir()
    assert tu.run_main('check') == 0
    st = tu.load_status()
    assert st['job']['state'] == 'failed'
    assert '中断' in st['job']['message'] and '断电' not in st['job']['message']
    assert st['check_error'] == '没有配置更新地址'          # and the check itself still ran
    assert not (tu.CLIENT / '1.2.0.new').exists()


@pytest.mark.parametrize('cmd', [('run',), ('install', '1.2.0')])
def test_run_and_install_clear_it_too(tu, cmd):
    job_left(tu, 'installing')
    tu.run_main(*cmd)
    st = tu.load_status()
    # install then fails on its own (no sources): either way nothing is left "running"
    assert st['job']['state'] == 'failed'


def test_killed_during_self_test_tests_again_and_keeps_it(tu):
    install_dirs(tu, '1.1.0', '1.2.0')
    tu.set_link('previous', '1.1.0')
    tu.set_link('current', '1.2.0')
    tu.PENDING.write_text(json.dumps({'from': '1.1.0', 'to': '1.2.0', 'previous': None,
                                      'need_hub': False}))
    job_left(tu, 'testing')
    seconds = []
    tu.self_test = lambda version, need_hub, s: seconds.append(s) or (True, '')
    tu.run_main('check')
    st = tu.load_status()
    assert st['job']['state'] == 'done' and st['current'] == '1.2.0'
    assert seconds == [tu.TEST_SECONDS]
    assert not tu.PENDING.exists()


def test_killed_during_self_test_rolls_back_if_it_fails(tu):
    install_dirs(tu, '1.1.0', '1.2.0')
    tu.set_link('previous', '1.1.0')
    tu.set_link('current', '1.2.0')
    tu.PENDING.write_text(json.dumps({'from': '1.1.0', 'to': '1.2.0', 'previous': None,
                                      'need_hub': False}))
    job_left(tu, 'testing')
    tu.self_test = lambda version, need_hub, s: (version == '1.1.0', '新版本没有启动起来')
    tu.run_main('check')
    assert tu.load_status()['job']['state'] == 'rolled_back'
    assert tu.current_version() == '1.1.0'


def test_pending_from_before_the_switch_is_dropped(tu):
    install_dirs(tu, '1.1.0')
    tu.set_link('current', '1.1.0')
    tu.PENDING.write_text(json.dumps({'from': '1.1.0', 'to': '1.2.0', 'previous': None,
                                      'need_hub': False}))
    job_left(tu, 'installing')
    tu.run_main('check')
    assert not tu.PENDING.exists()
    assert tu.load_status()['job']['state'] == 'failed'
    assert tu.current_version() == '1.1.0'


@pytest.mark.parametrize('state', ['done', 'failed', 'rolled_back'])
def test_a_finished_job_is_left_alone(tu, state):
    job_left(tu, state)
    tu.run_main('check')
    assert tu.load_status()['job']['state'] == state


def test_boot_says_power_cut(tu):
    job_left(tu, 'downloading')
    tu.run_main('boot')
    st = tu.load_status()
    assert st['job']['state'] == 'failed' and '断电或重启' in st['job']['message']
    assert 'check_error' not in st                        # boot doesn't check


# ---- releases without signatures (2026-11-01): https only, manifest shape, sha256 of every file ----


def test_only_https_or_this_machine():
    m = load_module()
    assert m.allowed_url('https://github.com/o/r/releases/')
    assert m.allowed_url('http://127.0.0.1:18432/') and m.allowed_url('http://localhost:1/')
    assert not m.allowed_url('http://github.com/o/r/releases/')
    assert not m.allowed_url('http://192.168.1.2:8000/')
    assert not m.allowed_url('ftp://127.0.0.1/')


def load_module():
    loader = importlib.machinery.SourceFileLoader('thinksmart_update_plain', str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def test_plain_http_source_is_refused(tu):
    tu.CONF.write_text('SOURCES="http://example.com/releases/"\n')
    tu.run_main('check')
    assert tu.load_status()['check_error'] == '更新地址必须是 https'


@pytest.fixture
def source(tu, tmp_path):
    """A release served over http://127.0.0.1 (allowed for test sources), as make-release.py lays it out."""
    root = tmp_path / 'www'
    rel = root / 'download' / 'client-v1.2.0'
    rel.mkdir(parents=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as t:
        data = b"__version__ = '1.2.0'\n"
        info = tarfile.TarInfo('thinksmart_client/__init__.py')
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    code = buf.getvalue()
    (rel / 'thinksmart-client-1.2.0.tar.gz').write_bytes(code)
    env_id = '0123456789abcdef'
    # the environment is already on the device: only the code is downloaded
    (tu.ENVS / env_id / 'bin').mkdir(parents=True)
    (tu.ENVS / env_id / 'bin' / 'python').symlink_to(sys.executable)
    manifest = {'component': 'client', 'version': '1.2.0', 'published': '2026-11-01T10:00:00+08:00',
                'notes': ['测试'], 'files': {
                    'code': {'path': 'client-v1.2.0/thinksmart-client-1.2.0.tar.gz',
                             'sha256': hashlib.sha256(code).hexdigest(), 'size': len(code)},
                    'env': {'id': env_id, 'path': 'client-v1.2.0/client-env.tar.gz',
                            'sha256': '0' * 64, 'size': 1}}}
    latest = root / 'latest' / 'download'
    latest.mkdir(parents=True)

    def publish(**changes):
        m = json.loads(json.dumps(manifest))
        for k, v in changes.items():
            m[k] = v
        (latest / 'latest.json').write_text(json.dumps(m))
        return m
    publish()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tu.CONF.write_text(f'SOURCES="http://127.0.0.1:{server.server_port}/"\n')
    install_dirs(tu, '1.1.0')
    tu.set_link('current', '1.1.0')
    yield {'publish': publish, 'code': rel / 'thinksmart-client-1.2.0.tar.gz', 'manifest': manifest}
    server.shutdown()


def test_check_finds_an_unsigned_release(tu, source):
    tu.run_main('check')
    st = tu.load_status()
    assert st['check_error'] is None and st['available']['version'] == '1.2.0'


@pytest.mark.parametrize('bad', [
    {'version': '../../etc'},                               # becomes a directory name
    {'files': {'code': {'path': '/etc/passwd', 'sha256': '0' * 64, 'size': 1},
               'env': {'id': '0123456789abcdef', 'path': 'x', 'sha256': '0' * 64, 'size': 1}}},
    {'files': {'code': {'path': 'a', 'sha256': '0' * 64, 'size': 1},
               'env': {'id': '../../tmp', 'path': 'x', 'sha256': '0' * 64, 'size': 1}}},
    {'component': 'hub'},
])
def test_a_malformed_manifest_is_ignored(tu, source, bad):
    source['publish'](**bad)
    tu.run_main('check')
    st = tu.load_status()
    assert st['check_error'] == '更新服务器上的版本信息不对，已忽略' and not st.get('available')


def test_install_checks_sha256(tu, source):
    tu.run_main('check')
    code = bytearray(source['code'].read_bytes())
    code[len(code) // 2] ^= 1                               # same size, one bit off
    source['code'].write_bytes(bytes(code))
    assert tu.run_main('install', '1.2.0') == 1
    st = tu.load_status()
    assert st['job']['state'] == 'failed' and '校验不对' in st['job']['message']
    assert tu.current_version() == '1.1.0' and not (tu.CLIENT / '1.2.0').exists()
    assert not list(tu.DOWNLOAD.glob('*'))


def test_install_from_a_test_source(tu, source):
    tu.run_main('check')
    assert tu.run_main('install', '1.2.0') == 0
    assert tu.load_status()['job']['state'] == 'done'
    assert tu.current_version() == '1.2.0' and tu.previous_version() == '1.1.0'
    assert (tu.CLIENT / '1.2.0' / 'thinksmart_client' / '__init__.py').exists()


def test_an_older_manifest_is_not_taken(tu, source):
    tu.run_main('check')
    source['publish'](published='2026-10-01T10:00:00+08:00')
    tu.run_main('check')
    assert tu.load_status()['check_error'] == '连不上更新服务器'


def test_a_cut_download_fails_cleanly_in_chinese(tu, source, monkeypatch):
    """IncompleteRead (the connection dropped half way) is not an OSError: it used to escape the
    retry and show its English text on the page."""
    import http.client
    tu.run_main('check')
    real = tu.opener
    calls = []

    class Cut:
        def open(self, req, timeout=None):
            calls.append(req.full_url)
            if '/download/client-v' not in req.full_url:
                return real(tu.read_conf()).open(req, timeout=timeout)
            raise http.client.IncompleteRead(b'part', 100)
    monkeypatch.setattr(tu, 'opener', lambda conf: Cut())
    assert tu.run_main('install', '1.2.0') == 1
    msg = tu.load_status()['job']['message']
    assert msg.startswith('下载失败') and 'IncompleteRead' not in msg and 'bytes' not in msg
    assert tu.current_version() == '1.1.0'
