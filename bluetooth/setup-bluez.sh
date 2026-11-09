#!/bin/sh
# BlueZ settings for the speaker role (main.conf has no drop-in directory in BlueZ 5.72). The device class
# stays BlueZ's own (its hostname plugin sets it from the chassis, a tablet: main.conf's Class is overridden).
#   JustWorksRepairing = always a phone that forgot the device can pair again (only ever while the
#                               user has opened the 3-minute "add a phone" window: pairing is off otherwise)
# Run as root; runs again harmlessly.
set -e
CONF=/etc/bluetooth/main.conf
set_key() {  # key value: replace "#key = ..." or "key = ..." under [General], else append after [General]
    if grep -Eq "^#? *$1 *=" "$CONF"; then
        sed -i -E "0,/^#? *$1 *=.*/s//$1 = $2/" "$CONF"
    else
        sed -i "/^\[General\]/a $1 = $2" "$CONF"
    fi
}
set_key JustWorksRepairing always
