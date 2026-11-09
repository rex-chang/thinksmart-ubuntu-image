-- ThinkSmart View as a Bluetooth speaker (phones play to it; it never plays to headphones).
-- Installed to /etc/wireplumber/bluetooth.lua.d/, loaded after Ubuntu's 50-bluez-config.lua.
--
-- Only the A2DP sink role: no hands-free/headset profiles, or a paired phone would route its calls
-- (and their microphone) to the device. The voice client takes care of pairing, the on/off switch
-- and playback.
--
-- media-source-role = input: what a phone streams becomes an audio source the voice client records
-- and mixes into its own output, instead of a playback stream WirePlumber links straight to the
-- speaker. The client's echo canceller only knows what the client plays: music that bypassed it
-- would make the wake word nearly deaf while a phone plays.
bluez_monitor.properties["bluez5.roles"] = "[ a2dp_sink ]"
bluez_monitor.properties["bluez5.hfphsp-backend"] = "none"

-- media-source-role is read when the phone's node is created (WirePlumber's bluez.lua passes the
-- node rules' properties to it), not from the monitor's properties. A source is not linked anywhere
-- by itself: the client records it.
table.insert(bluez_monitor.rules, {
  matches = { { { "node.name", "matches", "bluez_input.*" } } },
  apply_properties = {
    ["bluez5.media-source-role"] = "input",
    ["node.autoconnect"] = false,
    -- never suspended for being idle: that would release the stream, and a phone that was suspended
    -- keeps showing "playing" without sending anything until it is paused and played again
    ["session.suspend-timeout-seconds"] = 0,
    -- a phone's stream must never become the default microphone
    ["priority.session"] = 0,
    ["priority.driver"] = 0,
  },
})
