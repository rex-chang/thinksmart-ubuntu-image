#!/bin/bash
# Run a command on the device over SSH with a dedicated key (used by image/pull-from-device.sh and
# image/restore-userdata.sh). Usage: ./ts.sh 'command'
# TS_HOST defaults to 172.16.42.1, the USB-cable network, whose address is fixed. Set
# TS_HOST=<device IP> for Wi-Fi, whose address the router assigns and changes. The host key of
# whichever address is used has to be in the known hosts file below.
# The key is ~/.ssh/thinksmart_ed25519 and its known hosts ~/.ssh/known_hosts_thinksmart (see README section 4).
exec ssh -o BatchMode=yes -o ConnectTimeout=10 \
  -o UserKnownHostsFile="$HOME/.ssh/known_hosts_thinksmart" \
  -i "$HOME/.ssh/thinksmart_ed25519" \
  "ubuntu@${TS_HOST:-172.16.42.1}" "$@"
