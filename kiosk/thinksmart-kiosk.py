#!/usr/bin/env python3
"""Full-screen single-page browser for the ThinkSmart kiosk session (runs inside Weston).

Reads URL= from /etc/thinksmart-kiosk.conf. Cookies and site data persist in
~/.local/share/thinksmart-kiosk so logins survive reboots.

While the URL can't be loaded (nothing installed there yet, the service is starting or
was removed, no network) the built-in welcome page is shown instead of WebKit's error
page, and the URL is retried in the background until it answers. A device that has never
been on a network (fresh image: no saved Wi-Fi, no address) also stays on the welcome page,
where Wi-Fi is set up on the screen, even though the local voice client would load.
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import gi

gi.require_version('Gtk', '3.0')
gi.require_version('WebKit2', '4.1')
from gi.repository import GLib, Gtk, WebKit2  # noqa: E402

CONF = '/etc/thinksmart-kiosk.conf'
SHARE = '/usr/local/share/thinksmart-kiosk'
WELCOME = SHARE + '/welcome.html'
KEYBOARD = SHARE + '/keyboard.js'
RETRY_SECONDS = 5
_HOSTNAME = socket.gethostname()


def read_conf():
    conf = {}
    try:
        with open(CONF) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                v = value.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                    v = v[1:-1]
                conf[key.strip()] = v
    except OSError:
        pass
    return conf


def device_status(include_ssid=True):
    """What the welcome page shows about the device: addresses, host name, Wi-Fi."""
    addrs = []
    try:
        out = subprocess.run(['ip', '-4', '-o', 'addr', 'show', 'scope', 'global'],
                             capture_output=True, text=True, timeout=2).stdout
        addrs = [(line.split()[1], line.split()[3].split('/')[0]) for line in out.splitlines()]
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    # usb0 is the USB-cable network (the computer on the other end gets 172.16.42.2); it is
    # always up, so it doesn't mean the device is online and is shown only as a rescue hint
    ips = [ip for iface, ip in addrs if not iface.startswith('usb')]
    usb = next((ip for iface, ip in addrs if iface.startswith('usb')), '')
    ssid = ''
    if include_ssid:
        try:
            out = subprocess.run(['nmcli', '-t', '-f', 'ACTIVE,SSID', 'dev', 'wifi', 'list', '--rescan', 'no'],
                                 capture_output=True, text=True, timeout=2).stdout
            ssid = next((nmcli_fields(line)[1] for line in out.splitlines() if line.startswith('yes:')), '')
            if not ssid:
                ssid = _active_wifi_ssid()
        except (OSError, subprocess.SubprocessError):
            pass
    return {'ip': ','.join(ips), 'usb': usb, 'host': _HOSTNAME, 'ssid': ssid}


def nmcli(*args, timeout=10):
    return subprocess.run(['nmcli', *args], capture_output=True, text=True, timeout=timeout)


def nmcli_fields(line):
    """Split one line of `nmcli -t` output (':' separated, '\\:' escaped)."""
    if '\\' not in line:
        return line.split(':')
    fields, field = [], []
    i = 0
    while i < len(line):
        char = line[i]
        if char == '\\' and i + 1 < len(line) and line[i + 1] in ('\\', ':'):
            i += 1
            field.append(line[i])
        elif char == ':':
            fields.append(''.join(field))
            field.clear()
        else:
            field.append(char)
        i += 1
    fields.append(''.join(field))
    return fields


def connection_uuids(active=False):
    out = nmcli('-t', '-f', 'UUID,TYPE', 'connection', 'show', *(['--active'] if active else [])).stdout
    return {f[0]: f[1] for f in map(nmcli_fields, out.splitlines()) if len(f) >= 2}


def _nmcli_multi_ssid(wifi_uuids):
    raw = nmcli('-g', 'connection.uuid,802-11-wireless.ssid', 'connection', 'show', 'uuid', *wifi_uuids).stdout
    uuid_set = set(wifi_uuids)
    by_uuid = {}
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        u = lines[i]
        if u in uuid_set and u not in by_uuid:
            nxt = lines[i + 1] if i + 1 < len(lines) else ''
            if nxt in uuid_set:
                by_uuid[u] = ''
                i += 1
            else:
                by_uuid[u] = nxt
                i += 2
        else:
            i += 1
    return [by_uuid.get(u, '') for u in wifi_uuids]


def _active_wifi_ssid():
    act = [u for u, kind in connection_uuids(active=True).items() if kind == '802-11-wireless']
    if not act:
        return ''
    raw = _nmcli_multi_ssid(act[:1])
    return nmcli_fields(raw[0])[0] if raw and raw[0] else ''


def wifi_profiles(uuids=None):
    """Saved Wi-Fi networks (NetworkManager keeps their passwords): SSID -> profile UUID."""
    wifi_uuids = [u for u, kind in (uuids if uuids is not None else connection_uuids()).items()
                  if kind == '802-11-wireless']
    if not wifi_uuids:
        return {}
    out = _nmcli_multi_ssid(wifi_uuids)
    return {nmcli_fields(s)[0]: u for u, s in zip(wifi_uuids, out) if s}


def needs_network_setup():
    """Fresh device: no saved Wi-Fi network and no address on any other interface (USB aside)."""
    try:
        return not device_status(include_ssid=False)['ip'] and '802-11-wireless' not in connection_uuids().values()
    except (OSError, subprocess.SubprocessError):
        return False


def wifi_status(_msg, _progress):
    """The network Wi-Fi is on now (empty ssid: none), for the client's settings page."""
    try:
        out = nmcli('-t', '-f', 'ACTIVE,SSID,SIGNAL', 'dev', 'wifi', 'list', '--rescan', 'no', timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {'ok': False}
    for f in map(nmcli_fields, out.splitlines()):
        if len(f) >= 3 and f[0] == 'yes':
            ssid = f[1] or _active_wifi_ssid()
            return {'ok': True, 'ssid': ssid, 'signal': int(f[2]) if f[2].isdigit() else 0}
    return {'ok': True, 'ssid': '', 'signal': 0}


def wifi_scan(_msg, _progress):
    try:
        out = nmcli('-t', '-f', 'IN-USE,SSID,SIGNAL,SECURITY', 'dev', 'wifi', 'list', '--rescan', 'yes',
                    timeout=25).stdout
        saved = wifi_profiles()
    except (OSError, subprocess.SubprocessError):
        return {'ok': False}
    best = {}  # one entry per SSID: the connected one, else the strongest access point
    for line in out.splitlines():
        f = nmcli_fields(line)
        if len(f) < 4:
            continue
        ssid = f[1] or (_active_wifi_ssid() if f[0] == '*' else '')
        if not ssid:
            continue
        n = {'ssid': ssid, 'signal': int(f[2]) if f[2].isdigit() else 0, 'secure': f[3] not in ('', '--'),
             'active': f[0] == '*', 'saved': ssid in saved}
        old = best.get(n['ssid'])
        if old is None or n['active'] or (not old['active'] and n['signal'] > old['signal']):
            best[n['ssid']] = n
    return {'ok': True, 'networks': sorted(best.values(), key=lambda n: (not n['active'], not n['saved'], -n['signal']))}


def reconnect_later(uuid):
    """After a failed attempt, bring the previous network back without making the user wait for it."""
    def work():
        try:
            nmcli('connection', 'up', 'uuid', uuid, timeout=45)
        except (OSError, subprocess.SubprocessError):
            pass
    threading.Thread(target=work, daemon=True).start()


def supplicant_state(path):
    """wpa_supplicant's state for wlan0 (scanning, associating, 4way_handshake, completed, ...)."""
    try:
        out = subprocess.run(['busctl', 'get-property', 'fi.w1.wpa_supplicant1', path,
                              'fi.w1.wpa_supplicant1.Interface', 'State'],
                             capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return ''
    return out.split('"')[1] if out.count('"') >= 2 else ''


_SUPPLICANT_PATH = ''


def supplicant_path(refresh=False):
    global _SUPPLICANT_PATH
    if _SUPPLICANT_PATH and not refresh:
        return _SUPPLICANT_PATH
    try:
        out = subprocess.run(['busctl', 'call', 'fi.w1.wpa_supplicant1', '/fi/w1/wpa_supplicant1',
                              'fi.w1.wpa_supplicant1', 'GetInterface', 's', 'wlan0'],
                             capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return ''
    _SUPPLICANT_PATH = out.split('"')[1] if out.count('"') >= 2 else ''
    return _SUPPLICANT_PATH


# A good password finishes the 4-way handshake in ~0.1 s (17 joins on this device: median 0.10 s,
# slowest 0.83 s); with a wrong one wpa_supplicant waits 10 s before giving up. Access points
# resend a lost handshake frame about once a second and give up after ~4 tries, so a handshake
# still stuck after 5 s won't succeed whatever the signal: call it a wrong password then.
HANDSHAKE_GIVE_UP = 5.0
STAGES = {'scanning': 'scan', 'authenticating': 'join', 'associating': 'join', 'associated': 'join',
          '4way_handshake': 'auth', 'group_handshake': 'auth', 'completed': 'ip'}


def run_nmcli_watched(cmd, progress):
    """Run an nmcli activation while reporting wpa_supplicant's stages through progress(stage).

    Returns (ok, error text). Stops early once the handshake hangs (wrong password).
    """
    path = supplicant_path()
    p = subprocess.Popen(['nmcli', *cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 50
    stage = handshake_since = None
    left_old = False  # 'completed' only counts once the old network has been dropped
    while p.poll() is None:
        state = supplicant_state(path) if path else ''
        if not state and path:
            path = supplicant_path(refresh=True)
            state = supplicant_state(path) if path else ''
        left_old = left_old or (state not in ('', 'completed'))
        new_stage = STAGES.get(state) if left_old else None
        if new_stage and new_stage != stage:
            stage = new_stage
            progress(stage)
        if state in ('4way_handshake', 'group_handshake'):
            handshake_since = handshake_since or time.monotonic()
            if time.monotonic() - handshake_since > HANDSHAKE_GIVE_UP:
                break
        else:
            handshake_since = None
        if time.monotonic() > deadline:
            break
        time.sleep(0.25)
    if p.poll() is None:
        wrong = handshake_since is not None
        p.terminate()
        try:
            p.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
        try:
            nmcli('device', 'disconnect', 'wlan0')  # stop the activation NetworkManager is still running
        except (OSError, subprocess.SubprocessError):
            pass
        return False, 'Secrets were required' if wrong else 'Timeout'
    out, err = p.communicate()
    return p.returncode == 0, (err or out).strip()


def wifi_connect(msg, progress):
    """Join a network: with the saved password when no new one is given, else with the new one."""
    ssid, password = str(msg.get('ssid') or ''), str(msg.get('password') or '')
    if not 0 < len(ssid.encode()) <= 32 or (password and not 8 <= len(password) <= 63):
        return {'ok': False, 'reason': 'invalid'}
    try:
        before = connection_uuids()
        previous = next((u for u, t in connection_uuids(active=True).items() if t == '802-11-wireless'), None)
        saved = wifi_profiles(before).get(ssid)
        # `dev wifi connect` writes a typed password into the saved profile; keep the old one so a
        # typo doesn't cost the network that was working
        # 2026-11-07 Claude: an empty psk read does not mean an open network (a failed read or an
        # agent-owned secret also reads empty); decide by key-mgmt, which needs no secrets, so a
        # failed join never strips the security off a WPA network that was working
        old_psk = None
        old_km = None
        was_open = False
        if saved and (password or msg.get('hidden')):
            km = nmcli('-g', '802-11-wireless-security.key-mgmt', 'connection', 'show', 'uuid', saved)
            if km.returncode == 0 and not km.stdout.strip():
                was_open = True
            elif km.returncode == 0:
                old_km = km.stdout.strip()
                r = nmcli('-s', '-g', '802-11-wireless-security.psk', 'connection', 'show', 'uuid', saved)
                raw_psk = r.stdout.rstrip('\r\n') if r.returncode == 0 else ''
                old_psk = nmcli_fields(raw_psk)[0] if raw_psk else None
    except (OSError, subprocess.SubprocessError):
        return {'ok': False, 'reason': 'failed'}
    if saved and not password and not msg.get('hidden'):
        cmd = ['--wait', '40', 'connection', 'up', 'uuid', saved]
    else:
        cmd = ['--wait', '40', 'dev', 'wifi', 'connect', ssid]
        if password:
            cmd += ['password', password]
        if msg.get('hidden'):
            cmd += ['hidden', 'yes']
    ok, err = run_nmcli_watched(cmd, progress)
    if ok:
        return {'ok': True}
    reason =('password' if 'Secrets were required' in err or 'no-secrets' in err or 'password' in err.lower() else
              'notfound' if 'No network with SSID' in err else
              'timeout' if 'Timeout' in err or 'timed out' in err else 'failed')
    # Don't keep a new profile with a wrong password around; put a saved one's password back
    try:
        new_uuids = list(set(connection_uuids()) - set(before))
        if new_uuids:
            nmcli('connection', 'delete', 'uuid', *new_uuids)
        if old_km and old_psk:
            nmcli('connection', 'modify', 'uuid', saved, 'wifi-sec.key-mgmt', old_km, 'wifi-sec.psk', old_psk)
        elif old_psk:
            nmcli('connection', 'modify', 'uuid', saved, 'wifi-sec.psk', old_psk)
        elif was_open:
            nmcli('connection', 'modify', 'uuid', saved, 'remove', '802-11-wireless-security')
    except (OSError, subprocess.SubprocessError):
        pass
    if previous and (previous != saved or password or msg.get('hidden')):
        reconnect_later(previous)
    return {'ok': False, 'reason': reason, 'detail': err.splitlines()[-1][:120] if err else ''}


def wifi_forget(msg, _progress):
    ssid = str(msg.get('ssid') or '')
    if not ssid:
        return {'ok': False}
    try:
        wifi_uuids = [u for u, kind in connection_uuids().items() if kind == '802-11-wireless']
        if not wifi_uuids:
            return {'ok': True}
        out = _nmcli_multi_ssid(wifi_uuids)
        del_uuids = [u for u, s in zip(wifi_uuids, out) if s and nmcli_fields(s)[0] == ssid]
        if del_uuids and nmcli('connection', 'delete', 'uuid', *del_uuids).returncode != 0:
            return {'ok': False}
    except (OSError, subprocess.SubprocessError):
        return {'ok': False}
    return {'ok': True}


UPDATE_STATE = '/var/lib/thinksmart-update'


def update_status(_msg, _progress):
    """What the root updater (thinksmart-update) found or is doing, for the welcome page."""
    try:
        with open(os.path.join(UPDATE_STATE, 'status.json')) as f:
            data = json.load(f)
        return {'ok': True, 'update': data} if isinstance(data, dict) else {'ok': False}
    except (OSError, ValueError):
        return {'ok': False}


def update_request(msg, _progress):
    """Ask the updater to install the versions the user tapped: the client's and/or the system
    layer's (it installs only what it checked itself)."""
    action = 'install' if msg.get('cmd') == 'update_install' else 'check'
    try:
        req_dir = os.path.join(UPDATE_STATE, 'requests')
        os.makedirs(req_dir, exist_ok=True)
        tmp = os.path.join(req_dir, f'.{action}.tmp')
        with open(tmp, 'w') as f:
            json.dump({'action': action, 'version': str(msg.get('version') or ''),
                       'system': str(msg.get('system') or '')}, f)
        os.replace(tmp, os.path.join(req_dir, action))
        return {'ok': True}
    except OSError:
        return {'ok': False}


def run_command(handler, msg, progress):
    try:
        r = handler(msg, progress)
        return r if isinstance(r, dict) else {'ok': False, 'reason': 'failed'}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'reason': 'timeout'}
    except Exception as e:
        print(f'thinksmart-kiosk: {msg.get("cmd")} failed: {e!r}', file=sys.stderr, flush=True)
        return {'ok': False, 'reason': 'failed', 'detail': str(e)[:120]}


LOOPBACK = ('127.0.0.1', 'localhost', '::1')
WIFI_COMMANDS = {'wifi_status': wifi_status, 'scan': wifi_scan, 'connect': wifi_connect, 'forget': wifi_forget}
WELCOME_COMMANDS = dict(WIFI_COMMANDS, update_status=update_status, update_install=update_request,
                        update_check=update_request)


def page_commands(uri, url):
    if uri.startswith('file://' + WELCOME):
        return WELCOME_COMMANDS
    page, conf = urllib.parse.urlparse(uri), urllib.parse.urlparse(url or '')
    if (page.scheme in ('http', 'https') and (page.scheme, page.netloc) == (conf.scheme, conf.netloc)
            and page.hostname in LOOPBACK):
        return WIFI_COMMANDS
    return {}


_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def reachable(url):
    """True once something answers at url (any HTTP status counts: the page itself decides)."""
    if not url:
        return False
    if url.startswith('file://'):
        return os.path.exists(urllib.parse.urlparse(url).path)
    try:
        with _NO_PROXY_OPENER.open(url, timeout=2):
            return True
    except urllib.error.HTTPError as e:
        e.close()
        return True
    except (OSError, ValueError):
        return False


def main():
    conf = read_conf()
    url = conf.get('URL', '')
    # Extra environment for the WebKit/Mesa renderer, e.g. WEBKIT_ENV="WEBKIT_FORCE_VBLANK_TIMER=1";
    # set before the web process is spawned so it inherits them
    for item in conf.get('WEBKIT_ENV', '').split():
        key, _, value = item.partition('=')
        if key:
            os.environ[key] = value

    data_dir = os.path.expanduser('~/.local/share/thinksmart-kiosk')
    cache_dir = os.path.expanduser('~/.cache/thinksmart-kiosk')
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)
    manager = WebKit2.WebsiteDataManager(base_data_directory=data_dir,
                                         base_cache_directory=cache_dir)
    manager.get_cookie_manager().set_persistent_storage(
        os.path.join(data_dir, 'cookies.sqlite'), WebKit2.CookiePersistentStorage.SQLITE)
    context = WebKit2.WebContext.new_with_website_data_manager(manager)

    # A kiosk has nobody to click "play": allow media with sound to autoplay
    policies = WebKit2.WebsitePolicies(autoplay=WebKit2.AutoplayPolicy.ALLOW)
    # The welcome page talks to us through window.webkit.messageHandlers.thinksmart (Wi-Fi setup)
    ucm = WebKit2.UserContentManager()
    ucm.register_script_message_handler('thinksmart')
    shell = 'window.thinksmartShell = {version: 2, keyboard: true, wifi: true};'
    try:
        with open(KEYBOARD, encoding='utf-8') as f:
            shell += '\n' + f.read()
    except OSError:
        shell = shell.replace('keyboard: true', 'keyboard: false')
    ucm.add_script(WebKit2.UserScript.new(shell, WebKit2.UserContentInjectedFrames.TOP_FRAME,
                                          WebKit2.UserScriptInjectionTime.START, None, None))
    for addon in ('/usr/local/share/thinksmart-home/home_overlay.js',):
        try:
            with open(addon, encoding='utf-8') as script:
                ucm.add_script(WebKit2.UserScript.new(script.read(), WebKit2.UserContentInjectedFrames.TOP_FRAME,
                                                      WebKit2.UserScriptInjectionTime.END, None, None))
        except OSError:
            pass
    view = WebKit2.WebView(web_context=context, website_policies=policies, user_content_manager=ucm)
    settings = view.get_settings()
    # Developer tools only when profiling: WEBKIT_ENV="... WEBKIT_INSPECTOR_HTTP_SERVER=127.0.0.1:9222",
    # then open http://127.0.0.1:9222 through an ssh tunnel
    settings.set_enable_developer_extras(bool(os.environ.get('WEBKIT_INSPECTOR_HTTP_SERVER')))
    settings.set_enable_smooth_scrolling(True)
    settings.set_media_playback_requires_user_gesture(False)
    # GPU=never|on-demand|always in the conf. CPU rendering is the fallback default; GPU needs
    # the freedreno GMEM fix (kernel-build/mesa) or FD_MESA_DEBUG=sysmem, see README.
    policy = {
        'on-demand': WebKit2.HardwareAccelerationPolicy.ON_DEMAND,
        'always': WebKit2.HardwareAccelerationPolicy.ALWAYS,
    }.get(conf.get('GPU', 'never'), WebKit2.HardwareAccelerationPolicy.NEVER)
    settings.set_hardware_acceleration_policy(policy)
    # WEBKIT_FEATURES="-Name +Name": switch WebKit feature flags (names from
    # WebKit2.Settings.get_all_features()), e.g. to turn off partial-repaint damage tracking
    feat_items = conf.get('WEBKIT_FEATURES', '').split()
    if feat_items:
        features = {}
        all_features = WebKit2.Settings.get_all_features()
        for i in range(all_features.get_length()):
            features[all_features.get(i).get_identifier()] = all_features.get(i)
        for item in feat_items:
            name = item.lstrip('+-')
            if name in features:
                settings.set_feature_enabled(features[name], not item.startswith('-'))
            else:
                print(f'thinksmart-kiosk: unknown WebKit feature {name}', flush=True)

    # busy: the Wi-Fi sheet is open on the welcome page, don't switch pages under the user's fingers
    state = {'welcome': False, 'tries': 0, 'checking': False, 'busy': False}

    def run_js(fn, obj):
        view.evaluate_javascript('window.%s && %s(%s)' % (fn, fn, json.dumps(obj, ensure_ascii=False)),
                                 -1, None, None, None, None, None)

    def on_message(_ucm, result):
        uri = view.get_uri() or ''
        commands = page_commands(uri, url)
        try:
            msg = json.loads(result.get_js_value().to_string())
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        if msg.get('cmd') == 'busy' and commands is WELCOME_COMMANDS:
            state['busy'] = bool(msg.get('value'))
            run_js('thinksmartReply', {'id': msg.get('id'), 'ok': True})
            if not state['busy']:
                check_target()
            return
        handler = commands.get(msg.get('cmd'))
        if handler is None:
            if commands:
                run_js('thinksmartReply', {'id': msg.get('id'), 'ok': False, 'reason': 'unknown'})
            return

        def progress(stage):
            GLib.idle_add(run_js, 'thinksmartProgress', {'id': msg.get('id'), 'stage': stage})

        def work():
            r = run_command(handler, msg, progress)
            r['id'] = msg.get('id')
            # the address and Wi-Fi name may have just changed on the welcome page (not after update polls)
            st = (page_status() if commands is WELCOME_COMMANDS
                  and not str(msg.get('cmd', '')).startswith('update_') else None)

            def done():
                if view.get_uri() != uri:
                    return False
                run_js('thinksmartReply', r)
                if st and commands is WELCOME_COMMANDS:
                    run_js('thinksmartStatus', st)
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    ucm.connect('script-message-received::thinksmart', on_message)

    def page_status():
        st = device_status()
        st.update(target=url, tries=state['tries'], help=conf.get('HELP_URL', ''))
        return st

    def show_welcome():
        state['welcome'] = True
        state['busy'] = False
        view.load_uri('file://' + WELCOME + '?' + urllib.parse.urlencode(page_status()))

    def on_load_failed(_view, _event, failing_uri, error):
        # Cancelled loads are our own navigations (e.g. switching to the welcome page)
        if (failing_uri.startswith('file://' + WELCOME) or
                error.matches(WebKit2.NetworkError.quark(), WebKit2.NetworkError.CANCELLED)):
            return False
        GLib.idle_add(show_welcome)
        return True  # no WebKit error page

    def check_target():
        """Every few seconds while the welcome page is up: retry the URL off the main thread."""
        if state['welcome'] and not state['checking']:
            state['checking'] = True

            def work():
                ok = reachable(url) and not needs_network_setup()
                st = page_status() if (not ok or state['busy']) else None

                def done():
                    state['checking'] = False
                    if not state['welcome']:
                        return False
                    if ok and not state['busy']:
                        state['welcome'] = False
                        view.load_uri(url)
                    elif not ok:
                        state['tries'] += 1
                        st['tries'] = state['tries']
                        run_js('thinksmartStatus', st)
                    elif st:
                        st['tries'] = state['tries']
                        run_js('thinksmartStatus', st)
                    return False
                GLib.idle_add(done)
            threading.Thread(target=work, daemon=True).start()
        return True

    def reload():
        if url and not needs_network_setup():
            state['welcome'] = False
            state['busy'] = False
            view.load_uri(url)
        else:
            show_welcome()
        return False

    def on_terminated(*_args):
        GLib.timeout_add_seconds(2, reload)

    view.connect('load-failed', on_load_failed)
    view.connect('web-process-terminated', on_terminated)
    view.connect('context-menu', lambda *_args: True)  # no long-press menu
    # No pinch zoom on any page: WebKitGTK zooms with its own two-finger gesture on the view,
    # whatever the page's viewport tag or touch-action say (both are iOS-only in WebKit). GDK
    # marks the first finger on the screen as emulating the pointer; touches that land while
    # it is down are dropped before WebKit and its gestures see them, so the zoom gesture never
    # gets a second finger. Taps, scrolling and dragging with one finger are unchanged.
    # (The field, not event.get_pointer_emulated(): that flag is lost in the copy Python gets.)
    view.connect('touch-event', lambda _view, event: not event.touch.emulating_pointer)
    GLib.timeout_add_seconds(RETRY_SECONDS, check_target)

    window = Gtk.Window(title='ThinkSmart Kiosk')
    window.add(view)
    window.connect('destroy', Gtk.main_quit)
    window.fullscreen()
    window.show_all()
    for _sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, _sig, lambda *_: (Gtk.main_quit(), False)[1])
    reload()
    Gtk.main()


if __name__ == '__main__':
    main()
