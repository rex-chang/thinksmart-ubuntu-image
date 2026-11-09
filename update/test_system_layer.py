"""System layer updates: the table decides what goes where, every destination is backed up first and
put back when the restarted services don't come up (or the power was cut), user configuration is
never overwritten, and one request installs the system layer before the client.

  uv run --no-project --with pytest python -m pytest -q update
"""
import functools
import hashlib
import http.server
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
from pathlib import Path

import pytest

from test_thinksmart_update import SCRIPT, load_module, tu, install_dirs, source  # noqa: F401  (fixtures)

REPO = SCRIPT.parent.parent


@pytest.fixture
def sysu(tu, tmp_path, monkeypatch):
    """tu with the system layer pointed into tmp_path and nothing restarted for real."""
    m = tu
    m.ROOT = tmp_path / 'root'
    m.ROOT.mkdir()
    m.SYSTEM = m.OPT / 'system'
    m.SYS_RECORD, m.SYS_BACKUP = m.SYSTEM / 'installed.json', m.SYSTEM / 'backup'
    m.SYS_PENDING = m.STATE / 'system-pending.json'
    m.own = lambda path, owner: None
    m.calls = []

    def run_cmd(*cmd, timeout=120):
        m.calls.append(cmd)
        if cmd[0] == 'sh':
            r = subprocess.run(cmd, capture_output=True, text=True)
            return r.returncode, r.stdout
        return 0, ''
    m.run_cmd = run_cmd
    m.as_user = lambda *cmd, timeout=120: (m.calls.append(('user',) + cmd), (0, ''))[1]
    m.pkg_installed = lambda name: name != 'not-installed-pkg'
    m.effects = []
    m.take_effect = lambda actions: m.effects.append(list(actions))
    m.system_self_test = lambda actions, need_hub, seconds, need_audio=True: (True, '')
    m.health = lambda: None
    return m


def make_tree(root, version='0.2.0', kiosk=b'kiosk v2\n', extra='', post=''):
    """A small system layer like the repository's: two files, a kept config, a file the post script
    edits."""
    (root / 'image').mkdir(parents=True, exist_ok=True)
    (root / 'kiosk').mkdir(exist_ok=True)
    (root / 'VERSION').write_text(version + '\n')
    (root / 'kiosk' / 'thinksmart-kiosk.py').write_bytes(kiosk)
    (root / 'kiosk' / 'welcome.html').write_text('<p>hi</p>\n')
    (root / 'kiosk' / 'thinksmart-kiosk.conf').write_text('URL=default\n')
    (root / 'kiosk' / 'idle.service').write_text('[Service]\n')
    (root / 'image' / 'system-files.txt').write_text(f'''# test table
require python3-gi
kiosk/thinksmart-kiosk.py /usr/local/bin/thinksmart-kiosk.py 755 root kiosk
kiosk/welcome.html /usr/local/share/thinksmart-kiosk/welcome.html 644 root kiosk
kiosk/thinksmart-kiosk.conf /etc/thinksmart-kiosk.conf 644 root keep
kiosk/idle.service /home/ubuntu/.config/systemd/user/idle-blanker.service 644 ubuntu user:idle-blanker.service
- /etc/bluetooth/main.conf - root bluetooth
{extra}''')
    (root / 'image' / 'system-post.sh').write_text(post)
    return root


def test_the_repository_table_reads():
    m = load_module()
    entries, requires = m.read_table(REPO)
    dests = [e[1] for e in entries]
    assert '/usr/local/bin/thinksmart-kiosk.py' in dests and '/usr/local/sbin/thinksmart-update' in dests
    assert {'libspa-0.2-bluetooth'} <= set(requires)
    # user configuration is only ever installed when missing
    keep = {e[1] for e in entries if 'keep' in e[4]}
    assert keep == {'/etc/thinksmart-kiosk.conf', '/etc/thinksmart-update.conf'}
    # every file device-setup.sh files used to install is still in the table
    for d in ('/etc/asound.conf', '/etc/wireplumber/main.lua.d/99-cd18781y-alsa.lua',
              '/home/ubuntu/.config/wireplumber/main.lua.d/99-cd18781y-alsa.lua',
              '/etc/wireplumber/bluetooth.lua.d/60-thinksmart-speaker.lua',
              '/usr/local/share/thinksmart-kiosk/keyboard.js', '/etc/sysctl.d/99-zram.conf'):
        assert d in dests
    assert (REPO / 'VERSION').read_text().strip()


@pytest.mark.parametrize('line', [
    'kiosk/welcome.html /usr/bin/evil 755 root none',                    # outside the allowed places
    'kiosk/welcome.html /usr/local/bin/../../bin/sh 755 root none',
    'kiosk/welcome.html /usr/local/bin/x 755 nobody none',
    'kiosk/welcome.html /usr/local/bin/x 755 root reboot-now',            # unknown action
    'kiosk/welcome.html /usr/local/bin/x 755 root system:a;rm',
    '../etc/passwd /usr/local/bin/x 755 root none',
    'kiosk/missing /usr/local/bin/x 755 root none',
])
def test_a_bad_table_line_is_refused(tmp_path, line):
    m = load_module()
    tree = make_tree(tmp_path / 't', extra=line + '\n')
    with pytest.raises(m.Fail):
        m.read_table(tree)


def test_system_apply_installs_and_records(sysu, tmp_path):
    tree = make_tree(tmp_path / 't')
    conf = sysu.target('/etc/thinksmart-kiosk.conf')
    conf.parent.mkdir(parents=True)
    conf.write_text('URL=mine\n')                                         # the user's own
    assert sysu.run_main('system-apply', str(tree)) == 0
    assert sysu.target('/usr/local/bin/thinksmart-kiosk.py').read_bytes() == b'kiosk v2\n'
    assert oct(sysu.target('/usr/local/bin/thinksmart-kiosk.py').stat().st_mode & 0o777) == '0o755'
    assert sysu.target('/home/ubuntu/.config/systemd/user/idle-blanker.service').exists()
    assert conf.read_text() == 'URL=mine\n'
    rec = sysu.system_record()
    assert rec['version'] == '0.2.0' and '/etc/thinksmart-kiosk.conf' not in rec['files']
    assert sysu.load_status()['system']['current'] == '0.2.0'
    # system-verify: intact, then a hand edit is caught
    assert sysu.system_verify() == 0
    sysu.target('/usr/local/bin/thinksmart-kiosk.py').write_text('edited')
    assert sysu.system_verify() == 1


def test_no_record_is_the_20261101_image(sysu):
    assert sysu.system_version() == '0.1.0'


@pytest.fixture
def release(sysu, tmp_path):
    """A test source with a client 1.1.0 (installed already) and a system layer 0.2.0."""
    root = tmp_path / 'www'
    rel = root / 'download' / 'c1.1.0-s0.2.0'
    rel.mkdir(parents=True)
    install_dirs(sysu, '1.1.0')
    sysu.set_link('current', '1.1.0')
    env_id = '0123456789abcdef'
    state = {}

    def publish(version='0.2.0', tree_kw=None, requires=None, tree_id=None, client='1.1.0'):
        tree = make_tree(tmp_path / f'src-{version}-{len(state)}', version=version, **(tree_kw or {}))
        state['n'] = len(state) + 1
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as t:
            for f in sorted(tree.rglob('*')):
                if f.is_file():
                    t.add(f, f.relative_to(tree).as_posix())
        pkg = rel / f'thinksmart-system-{version}.tar.gz'
        pkg.write_bytes(buf.getvalue())
        entries, req = sysu.read_table(tree)
        code = b'x'
        manifest = {'component': 'client', 'version': client, 'published': f'2026-11-07T10:0{state["n"]}:00+08:00',
                    'notes': [], 'files': {
                        'code': {'path': 'c/x.tar.gz', 'sha256': hashlib.sha256(code).hexdigest(), 'size': 1},
                        'env': {'id': env_id, 'path': 'c/e.tar.gz', 'sha256': '0' * 64, 'size': 1}},
                    'system': {'version': version, 'notes': ['系统层测试'], 'requires': requires or req,
                               'tree': tree_id or sysu.tree_id(tree, entries),
                               'file': {'path': f'c1.1.0-s0.2.0/{pkg.name}', 'size': pkg.stat().st_size,
                                        'sha256': hashlib.sha256(pkg.read_bytes()).hexdigest()}}}
        latest = root / 'latest' / 'download'
        latest.mkdir(parents=True, exist_ok=True)
        (latest / 'latest.json').write_text(json.dumps(manifest))
        return tree
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sysu.CONF.write_text(f'SOURCES="http://127.0.0.1:{server.server_port}/"\n')
    yield publish
    server.shutdown()


def device_files(m):
    return {str(p.relative_to(m.ROOT)): p.read_bytes() for p in m.ROOT.rglob('*') if p.is_file()}


def old_device(m):
    """What a 20261101 device has: an older shell, the user's kiosk config, BlueZ's main.conf."""
    for dest, data in (('/usr/local/bin/thinksmart-kiosk.py', b'kiosk v1\n'),
                       ('/etc/thinksmart-kiosk.conf', b'URL=mine\n'),
                       ('/etc/bluetooth/main.conf', b'[General]\n')):
        p = m.target(dest)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    os.chmod(m.target('/usr/local/bin/thinksmart-kiosk.py'), 0o755)


def test_check_shows_the_system_layer(sysu, release):
    release()
    sysu.run_main('check')
    st = sysu.load_status()
    assert st['check_error'] is None and st['available'] is None
    assert st['system']['current'] == '0.1.0'
    assert st['system']['available']['version'] == '0.2.0' and st['system']['available']['missing'] == []


def test_install_system_layer(sysu, release, tmp_path):
    old_device(sysu)
    mainconf = sysu.target('/etc/bluetooth/main.conf')
    release(tree_kw={'post': f'echo "JustWorksRepairing = always" >> {mainconf}\n'})
    sysu.run_main('check')
    (sysu.REQUESTS / 'install').write_text(json.dumps({'action': 'install', 'version': '', 'system': '0.2.0'}))
    assert sysu.run_main('run') == 0
    st = sysu.load_status()
    assert st['job'] == {**st['job'], 'state': 'done', 'component': 'system', 'version': '0.2.0'}
    assert st['system']['current'] == '0.2.0' and st['system']['available'] is None
    assert sysu.target('/usr/local/bin/thinksmart-kiosk.py').read_bytes() == b'kiosk v2\n'
    assert sysu.target('/etc/thinksmart-kiosk.conf').read_bytes() == b'URL=mine\n'      # kept
    # only what changed restarts: the screen, the new user unit, bluetooth (its conf was edited)
    assert sysu.effects == [['bluetooth', 'kiosk', 'reload', 'user:idle-blanker.service']]
    assert sysu.system_record()['version'] == '0.2.0'
    assert not sysu.SYS_PENDING.exists() and not (sysu.SYSTEM / 'new').exists()
    # the backup is the previous version's files
    index = json.loads((sysu.SYS_BACKUP / 'index.json').read_text())
    assert {i['dest']: i['present'] for i in index}['/usr/local/bin/thinksmart-kiosk.py'] is True


def test_failed_self_test_puts_every_file_back(sysu, release, tmp_path):
    old_device(sysu)
    mainconf = sysu.target('/etc/bluetooth/main.conf')
    release(tree_kw={'post': f'echo "JustWorksRepairing = always" >> {mainconf}\n'})
    before = device_files(sysu)
    mode = sysu.target('/usr/local/bin/thinksmart-kiosk.py').stat().st_mode
    sysu.run_main('check')
    # the new files fail, the old ones (after the rollback restarted everything again) pass
    sysu.system_self_test = lambda actions, need_hub, seconds, need_audio=True: (len(sysu.effects) > 1, '屏幕界面没有启动起来')
    assert sysu.install_system(sysu.read_conf(), '0.2.0') == 1
    st = sysu.load_status()
    assert st['job']['state'] == 'rolled_back' and st['job']['component'] == 'system'
    assert '屏幕界面没有启动起来' in st['job']['message'] and '已退回 0.1.0' in st['job']['message']
    assert device_files(sysu) == before                       # new files gone, edited ones back
    assert sysu.target('/usr/local/bin/thinksmart-kiosk.py').stat().st_mode == mode
    assert sysu.system_version() == '0.1.0' and st['system']['failed'] == '0.2.0'
    assert len(sysu.effects) == 2                             # restarted again with the old files
    assert not sysu.SYS_PENDING.exists()


def test_power_cut_while_applying_rolls_back_at_boot(sysu, release):
    old_device(sysu)
    release()
    before = device_files(sysu)
    sysu.run_main('check')

    def cut(*a, **k):
        raise KeyboardInterrupt        # the process dies half way through the files
    real = sysu.put_file
    n = []

    def put_file(*a):
        n.append(1)
        if len(n) == 2:
            cut()
        return real(*a)
    sysu.put_file = put_file
    with pytest.raises(KeyboardInterrupt):
        sysu.install_system(sysu.read_conf(), '0.2.0')
    assert sysu.SYS_PENDING.exists() and device_files(sysu) != before
    sysu.put_file = real
    sysu.run_main('boot')
    st = sysu.load_status()
    assert st['job']['state'] == 'rolled_back' and '断电或重启' in st['job']['message']
    assert device_files(sysu) == before and not sysu.SYS_PENDING.exists()


def test_missing_package_means_reflash(sysu, release):
    release(requires=['not-installed-pkg'])
    sysu.run_main('check')
    avail = sysu.load_status()['system']['available']
    assert avail['missing'] == ['not-installed-pkg']
    (sysu.REQUESTS / 'install').write_text(json.dumps({'action': 'install', 'system': '0.2.0'}))
    sysu.run_main('run')                                       # not offered: just checks again
    assert sysu.system_version() == '0.1.0' and sysu.effects == []
    assert sysu.install_system(sysu.read_conf(), '0.2.0') == 1
    assert '重刷镜像' in sysu.load_status()['job']['message']


def test_package_not_matching_the_manifest_is_refused(sysu, release):
    old_device(sysu)
    release(tree_id='f' * 64)
    before = device_files(sysu)
    sysu.run_main('check')
    assert sysu.install_system(sysu.read_conf(), '0.2.0') == 1
    assert '对不上' in sysu.load_status()['job']['message'] and device_files(sysu) == before


def test_one_request_system_first_then_client(sysu, release, monkeypatch):
    release(client='1.2.0')
    sysu.run_main('check')
    order = []
    sysu.install_system = lambda conf, v: order.append(('system', v)) or 0
    sysu.install = lambda conf, v: order.append(('client', v)) or 0
    (sysu.REQUESTS / 'install').write_text(json.dumps({'action': 'install', 'version': '1.2.0', 'system': '0.2.0'}))
    sysu.run_main('run')
    assert order == [('system', '0.2.0'), ('client', '1.2.0')]
    # a failed system layer stops there
    order.clear()
    sysu.install_system = lambda conf, v: order.append(('system', v)) or 1
    (sysu.REQUESTS / 'install').write_text(json.dumps({'action': 'install', 'version': '1.2.0', 'system': '0.2.0'}))
    assert sysu.run_main('run') == 1 and order == [('system', '0.2.0')]
    # an old page asks for the client only
    order.clear()
    (sysu.REQUESTS / 'install').write_text(json.dumps({'action': 'install', 'version': '1.2.0'}))
    sysu.run_main('run')
    assert order == [('client', '1.2.0')]


def test_a_version_dropped_file_is_removed_and_its_unit_disabled(sysu, tmp_path):
    tree = make_tree(tmp_path / 't')
    sysu.run_main('system-apply', str(tree))
    unit = sysu.target('/home/ubuntu/.config/systemd/user/idle-blanker.service')
    assert unit.exists()
    table = tree / 'image' / 'system-files.txt'
    table.write_text('\n'.join(l for l in table.read_text().splitlines() if 'idle-blanker' not in l) + '\n')
    (tree / 'VERSION').write_text('0.3.0\n')
    sysu.run_main('system-apply', str(tree))
    assert not unit.exists()
    assert ('user', 'systemctl', '--user', 'disable', '--now', 'idle-blanker.service') in sysu.calls


def test_selftest_does_not_wait_for_the_lock(sysu):
    import fcntl
    with open(sysu.LOCK, 'w') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        r = subprocess.run([sys.executable, str(SCRIPT), 'selftest'], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0 and 'thinksmart-update 2' in r.stdout


def test_make_release_carries_the_system_layer(tmp_path):
    """make-release.py --system: one manifest with both, the updater bundled for old devices, and a
    VERSION that didn't move with changed files stops it."""
    img = tmp_path / 'img'
    # without the repository's own system-v* tags: this test makes its own
    subprocess.run(['git', 'clone', '-q', '--no-tags', str(REPO), str(img)], check=True)
    # this branch's uncommitted work as it would be committed
    for f in subprocess.run(['git', '-C', str(REPO), 'ls-files', '-mo', '--exclude-standard'],
                            capture_output=True, text=True, check=True).stdout.split():
        (img / f).parent.mkdir(parents=True, exist_ok=True)
        (img / f).write_bytes((REPO / f).read_bytes())
    git = lambda *a: subprocess.run(['git', '-C', str(img), *a], check=True, capture_output=True)
    git('add', '-A')
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'wip', '--allow-empty')
    client = tmp_path / 'client'
    (client / 'thinksmart_client').mkdir(parents=True)
    (client / 'thinksmart_client' / '__init__.py').write_text("__version__ = '1.2.0'\n")
    (client / 'requirements.lock').write_text('x==1\n')
    subprocess.run(['git', 'init', '-q', str(client)], check=True)
    subprocess.run(['git', '-C', str(client), 'add', '-A'], check=True)
    cache = tmp_path / 'envs'
    cache.mkdir()
    env_id = hashlib.sha256(b'x==1\n').hexdigest()[:16]
    (cache / f'client-env-{env_id}.tar.gz').write_bytes(b'env')          # no container build
    out = tmp_path / 'out'
    mk = [sys.executable, str(REPO / 'update' / 'make-release.py'), str(client), str(out),
          '--system', str(img), '--note', '客户端改动', '--system-note', '系统层改动', '--env-cache', str(cache)]
    r = subprocess.run(mk, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    version = (img / 'VERSION').read_text().strip()
    tag = f'c1.2.0-s{version}'
    m = json.loads((out / 'latest' / 'download' / 'latest.json').read_text())
    assert m['version'] == '1.2.0' and m['system']['version'] == version
    assert m['system']['file']['path'] == f'{tag}/thinksmart-system-{version}.tar.gz'
    assert load_module().valid_system(m['system']) and load_module().valid_manifest(m)
    with tarfile.open(out / 'download' / tag / f'thinksmart-client-1.2.0.tar.gz') as t:
        assert 'deploy/updater/thinksmart-update' in t.getnames()
    with tarfile.open(out / 'download' / tag / f'thinksmart-system-{version}.tar.gz') as t:
        names = t.getnames()
        assert 'image/system-files.txt' in names and 'kiosk/thinksmart-kiosk.py' in names
    text = (out / f'release-{tag}.txt').read_text()
    assert '不需要手动下载' in text and 'thinksmart-ubuntu-image' in text
    # the same files again: fine; a changed file with the same VERSION: refused
    assert subprocess.run(mk, capture_output=True).returncode == 0
    (img / 'kiosk' / 'welcome.html').write_text('changed')
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qam', 'change')
    r = subprocess.run(mk, capture_output=True, text=True)
    assert r.returncode != 0 and 'raise VERSION' in r.stderr
    # uncommitted: refused too
    (img / 'kiosk' / 'welcome.html').write_text('changed again')
    r = subprocess.run(mk, capture_output=True, text=True)
    assert r.returncode != 0 and 'uncommitted' in r.stderr


# ---- power cuts: what recovery reads must be on disk before files change (review 11-07) ----


def test_backup_and_pending_are_on_disk_before_any_file_changes(sysu, release, monkeypatch):
    old_device(sysu)
    release()
    sysu.run_main('check')
    events = []
    monkeypatch.setattr(sysu.os, 'sync', lambda: events.append('sync'))
    real_put = sysu.put_file

    def put_file(*a):
        # by now the pending record and the whole backup must have been synced, in full
        events.append(('put', sysu.SYS_PENDING.stat().st_size,
                       (sysu.SYS_BACKUP / 'index.json').stat().st_size))
        return real_put(*a)
    sysu.put_file = put_file
    assert sysu.install_system(sysu.read_conf(), '0.2.0') == 0
    first_put = next(i for i, e in enumerate(events) if e != 'sync')
    assert 'sync' in events[:first_put]
    _, pending_size, index_size = events[first_put]
    assert pending_size > 0 and index_size > 0
    # the record is synced before the pending record is dropped
    assert json.loads(sysu.SYS_RECORD.read_text())['version'] == '0.2.0'
    assert events[-1] == 'sync'


def test_a_damaged_backup_copy_is_not_restored(sysu, release):
    """Power cut while the backup was still in the page cache: the copy came back empty. Restoring
    it would leave an empty kiosk.py; leaving the new file is the lesser harm."""
    old_device(sysu)
    mainconf = sysu.target('/etc/bluetooth/main.conf')
    release(tree_kw={'post': f'echo "JustWorksRepairing = always" >> {mainconf}\n'})
    sysu.run_main('check')
    index_of = {}

    def fail_after_damaging(actions, need_hub, seconds, need_audio=True):
        if len(sysu.effects) == 1:
            index = json.loads((sysu.SYS_BACKUP / 'index.json').read_text())
            i = next(n for n, it in enumerate(index) if it['dest'] == '/usr/local/bin/thinksmart-kiosk.py')
            index_of['kiosk'] = i
            (sysu.SYS_BACKUP / 'files' / str(i)).write_bytes(b'')
            return False, '屏幕界面没有启动起来'
        return True, ''
    sysu.system_self_test = fail_after_damaging
    assert sysu.install_system(sysu.read_conf(), '0.2.0') == 1
    assert sysu.target('/usr/local/bin/thinksmart-kiosk.py').read_bytes() == b'kiosk v2\n'   # not emptied
    assert mainconf.read_bytes() == b'[General]\n'                                          # the rest is back
    assert not sysu.target('/usr/local/share/thinksmart-kiosk/welcome.html').exists()
    assert sysu.load_status()['job']['state'] == 'rolled_back'


def test_client_switch_waits_for_its_files_on_disk(tu, source, monkeypatch):
    tu.run_main('check')
    events = []
    monkeypatch.setattr(tu.os, 'sync', lambda: events.append('sync'))
    real = tu.set_link
    tu.set_link = lambda name, v: (events.append((name, tu.PENDING.exists() and tu.PENDING.stat().st_size)),
                                   real(name, v))[1]
    assert tu.run_main('install', '1.2.0') == 0
    switch = events.index(next(e for e in events if e != 'sync' and e[0] == 'current'))
    assert 'sync' in events[:switch] and events[switch][1] > 0


def test_power_cut_after_a_passed_install_keeps_it(sysu, release, monkeypatch):
    """Installed, self-tested and recorded; the power went before dropping the pending record
    reached the disk. Boot must keep the new layer, not put the old files back under the new
    version's record."""
    old_device(sysu)
    release()
    sysu.run_main('check')
    real_unlink = Path.unlink

    def unlink(self, *a, **k):
        if self == sysu.SYS_PENDING:
            raise KeyboardInterrupt          # dies right here
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(Path, 'unlink', unlink)
    with pytest.raises(KeyboardInterrupt):
        sysu.install_system(sysu.read_conf(), '0.2.0')
    monkeypatch.setattr(Path, 'unlink', real_unlink)
    assert sysu.SYS_PENDING.exists() and sysu.system_record()['version'] == '0.2.0'
    after = device_files(sysu)
    sysu.run_main('boot')
    st = sysu.load_status()
    assert st['job']['state'] == 'done' and st['job']['component'] == 'system'
    assert st['system']['current'] == '0.2.0'
    assert device_files(sysu) == after and not sysu.SYS_PENDING.exists()
    assert sysu.system_verify() == 0
    assert len(sysu.effects) == 1                  # no rollback restarts


def test_an_interrupted_system_job_stays_a_system_job(sysu):
    sysu.save_status({'job': {'state': 'downloading', 'version': '0.2.0', 'component': 'system',
                              'message': '', 'progress': 0.3, 'time': 0}})
    sysu.run_main('boot')
    job = sysu.load_status()['job']
    assert job['state'] == 'failed' and job['component'] == 'system' and '中断' in job['message']


def test_sound_counts_only_if_it_worked_before(sysu, monkeypatch):
    """Client crashed (the welcome page shows): an update touching audio must not be rolled back
    for the sound the client couldn't give before either, or the client fix behind it never lands."""
    monkeypatch.setattr(sysu, 'health', lambda: None)
    assert sysu.system_checks(['wireplumber'], False, need_audio=True) == {'audio': None}
    assert sysu.system_checks(['wireplumber'], False, need_audio=False) == {}
    monkeypatch.setattr(sysu, 'health', lambda: {'audio': True, 'hub': True})
    assert sysu.system_checks(['mic'], True, need_audio=True) == {'audio': True}


def test_install_remembers_whether_sound_worked(sysu, release):
    old_device(sysu)
    release()
    sysu.run_main('check')
    seen = []
    sysu.health = lambda: None                      # the client is down before the update
    sysu.system_self_test = lambda actions, need_hub, seconds, need_audio=True: (seen.append(need_audio), (True, ''))[1]
    assert sysu.install_system(sysu.read_conf(), '0.2.0') == 0
    assert seen == [False]


def test_one_tap_for_both_reports_both(sysu, release):
    """The pages show the last job's message: after system layer + client it must name both."""
    release(client='1.2.0')
    sysu.run_main('check')
    sysu.install_system = lambda conf, v: (sysu.set_job(sysu.load_status(), 'done', v, f'系统层已更新到 {v}',
                                                        component='system'), 0)[1]
    sysu.install = lambda conf, v: (sysu.set_job(sysu.load_status(), 'done', v, f'已更新到 {v}'), 0)[1]
    (sysu.REQUESTS / 'install').write_text(json.dumps({'action': 'install', 'version': '1.2.0', 'system': '0.2.0'}))
    assert sysu.run_main('run') == 0
    assert sysu.load_status()['job']['message'] == '已更新到 系统层 0.2.0 · 客户端 1.2.0'
    # one of them alone: the message stays its own
    (sysu.REQUESTS / 'install').write_text(json.dumps({'action': 'install', 'version': '1.2.0'}))
    sysu.run_main('run')
    assert sysu.load_status()['job']['message'] == '已更新到 1.2.0'
