#!/usr/bin/env bash
# Deploy a COMPLETE client (app, web UI, Bluetooth) to the device through its own updater.
#
# CLIENT_DIR must be a clean checkout of the integrated client repository
# (<other>/xiaozhi-thinksmart-client-integration): every tracked file is packaged, so the device
# gets one coherent version. Nothing is copied over /opt/thinksmart/client/current.
#
# The release is built locally with update/make-release.py (client only: the tracked code plus the
# Python environment its requirements.lock names), copied to /var/tmp on the device, and installed
# by the device's own /usr/local/sbin/thinksmart-update with a temporary 127.0.0.1 source. That
# keeps the real update path: updater lock, interrupted-update recovery, sha256 checks, environment
# install, current/previous switch, health self-test and automatic rollback.
# /etc/thinksmart-update.conf is not touched, and user data (~/.config/thinksmart-client with the
# NetEase login, the hub's data/, the kiosk conf, Wi-Fi) is never copied.
#
#   CLIENT_DIR=../xiaozhi-thinksmart-client-integration ./netease/kiosk/deploy.sh
#
# HOST, SSH_OPTS (as ts.sh sets), OUT (build into this directory instead of a temp one),
# ENV_CACHE (make-release's environment cache) and DEPLOY_KEEP=1 (keep the remote staging) change
# the defaults.
set -euo pipefail

HOST=${HOST:-ubuntu@10.9.1.60}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
read -r -a SSH_OPTS <<< "${SSH_OPTS:-}"
SSH=(ssh "${SSH_OPTS[@]}")
SCP=(scp "${SSH_OPTS[@]}")

if [ -z "${CLIENT_DIR:-}" ]; then
    echo "error: set CLIENT_DIR to a clean checkout of the integrated client" >&2
    echo "       e.g. CLIENT_DIR=../xiaozhi-thinksmart-client-integration $0" >&2
    exit 2
fi
CLIENT_DIR=$(cd "$CLIENT_DIR" && pwd) || {
    echo "error: CLIENT_DIR=$CLIENT_DIR is not a directory" >&2
    exit 2
}
git -C "$CLIENT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
    echo "error: $CLIENT_DIR is not a git checkout of the integrated client" >&2
    exit 2
}
for required in thinksmart_client/__init__.py requirements.lock deploy/thinksmart-client.service; do
    git -C "$CLIENT_DIR" ls-files --error-unmatch "$required" >/dev/null 2>&1 || {
        echo "error: $CLIENT_DIR has no tracked $required; not a complete client checkout" >&2
        exit 2
    }
done
# make-release packages every tracked file from the working tree: a dirty checkout would silently
# drop untracked files and ship uncommitted ones, so only a committed tree is deployable.
if [ -n "$(git -C "$CLIENT_DIR" status --porcelain)" ]; then
    echo "error: $CLIENT_DIR has uncommitted or untracked changes; commit them first" >&2
    git -C "$CLIENT_DIR" status --short >&2
    exit 2
fi

VERSION=$(python3 - "$CLIENT_DIR" <<'PY'
import ast
import sys
from pathlib import Path
tree = ast.parse((Path(sys.argv[1]) / 'thinksmart_client' / '__init__.py').read_text())
for node in tree.body:
    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__version__' for t in node.targets):
        print(ast.literal_eval(node.value))
        break
PY
)
case "$VERSION" in
    ''|*[!0-9.]*|.*|*.) echo "error: bad client version in $CLIENT_DIR: '$VERSION'" >&2; exit 2 ;;
esac

"${SSH[@]}" "$HOST" 'test -x /usr/local/sbin/thinksmart-update' || {
    echo "error: $HOST has no /usr/local/sbin/thinksmart-update (image 20261101+ or the system layer)" >&2
    exit 1
}
CURRENT=$("${SSH[@]}" "$HOST" 'readlink /opt/thinksmart/client/current 2>/dev/null || true' | tr -d '[:space:]')
if [ -n "$CURRENT" ] && ! python3 - "$CURRENT" "$VERSION" <<'PY'
import sys
cur, new = (tuple(int(x) for x in v.split('.')) for v in sys.argv[1:3])
sys.exit(0 if new > cur else 1)
PY
then
    echo "error: client $VERSION is not newer than $HOST's current $CURRENT" >&2
    echo "       the updater only installs a newer version: bump __version__ in" >&2
    echo "       $CLIENT_DIR/thinksmart_client/__init__.py, commit it, then run this again" >&2
    exit 1
fi

STAMP=$(date +%F-%H%M%S)
if [ -n "${OUT:-}" ]; then
    RELEASE=$OUT
    mkdir -p "$RELEASE"
else
    RELEASE=$(mktemp -d "${TMPDIR:-/tmp}/thinksmart-client-release-$VERSION-$STAMP.XXXXXX")
fi
ENV_CACHE=${ENV_CACHE:-$HOME/.cache/thinksmart-release/envs}
echo "building client $VERSION from $CLIENT_DIR"
python3 "$ROOT/update/make-release.py" "$CLIENT_DIR" "$RELEASE" --env-cache "$ENV_CACHE"
[ -f "$RELEASE/latest/download/latest.json" ] || {
    echo "error: make-release did not produce $RELEASE/latest/download/latest.json" >&2
    exit 1
}
echo "release: $RELEASE"

STAGE=/var/tmp/thinksmart-client-deploy-$STAMP
"${SSH[@]}" "$HOST" "rm -rf '$STAGE' && mkdir -p '$STAGE'"
"${SCP[@]}" -r "$RELEASE/latest" "$RELEASE/download" "$HOST:$STAGE/"

DRIVER=$(mktemp "${TMPDIR:-/tmp}/thinksmart-client-install.XXXXXX")
trap 'rm -f "$DRIVER"' EXIT
cat > "$DRIVER" <<'PY'
#!/usr/bin/python3
"""Install the staged client release through the device's own updater.

Serves the staged release over http://127.0.0.1:<ephemeral>/ and calls
/usr/local/sbin/thinksmart-update 'install VERSION' in-process with a temporary configuration, so
the real update path runs unchanged: updater lock, interrupted-update recovery, sha256 checks,
environment install, current/previous switch, health self-test and rollback. The device's
/etc/thinksmart-update.conf is not modified.
"""
import json
import runpy
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STAGE = Path(sys.argv[1]).resolve()
VERSION = sys.argv[2]
UPDATER = Path('/usr/local/sbin/thinksmart-update')
STATUS = Path('/var/lib/thinksmart-update/status.json')

manifest = json.loads((STAGE / 'latest' / 'download' / 'latest.json').read_text())
if manifest.get('version') != VERSION:
    sys.exit(f'staged manifest is {manifest.get("version")}, expected {VERSION}')


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STAGE), **kwargs)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
conf = STAGE / 'thinksmart-update.conf'
conf.write_text(f'SOURCES=http://127.0.0.1:{server.server_port}/\nPROXY=\n')

module = runpy.run_path(str(UPDATER), run_name='deployment')
module['main'].__globals__['CONF'] = conf
sys.argv = ['thinksmart-update', 'install', VERSION]
try:
    rc = module['main']()
finally:
    server.shutdown()
    conf.unlink(missing_ok=True)

try:
    status = json.loads(STATUS.read_text())
except (OSError, ValueError):
    status = {}
job = status.get('job') or {}
print(f"job: {job.get('state')} {job.get('version')} {job.get('message')}".rstrip())
print(f"current: {status.get('current')}  previous: {module['previous_version']()}")
sys.exit(rc or 0)
PY
"${SCP[@]}" "$DRIVER" "$HOST:$STAGE/install.py"

echo "installing client $VERSION on $HOST through thinksmart-update (self-test + rollback included)"
if "${SSH[@]}" "$HOST" "sudo python3 '$STAGE/install.py' '$STAGE' '$VERSION'"; then
    if [ "${DEPLOY_KEEP:-0}" != 1 ]; then
        "${SSH[@]}" "$HOST" "sudo rm -rf '$STAGE'"
    fi
    echo "client $VERSION deployed on $HOST; /opt/thinksmart/client/previous keeps the old version"
else
    echo "error: install failed on $HOST; the updater kept or rolled back the previous version" >&2
    echo "       staging kept at $HOST:$STAGE" >&2
    echo "       status: ssh $HOST 'sudo cat /var/lib/thinksmart-update/status.json'" >&2
    exit 1
fi
