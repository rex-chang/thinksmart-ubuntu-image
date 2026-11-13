#!/usr/bin/env python3
"""
Standalone VCNL4200 Ambient Light Daemon

Publishes ambient light (lux) to /tmp/als_lux, where idle-blanker.py picks it up
when the user has brightness set to "auto". The chip is the same VCNL4200 the
proximity daemon talks to: ALS and PS live in different registers and run
concurrently, so both daemons can share the sensor.

Why userland instead of the kernel driver: the light-sensor@51 node in this
device tree is disabled and CONFIG_VCNL4000 is not built into the kernel, so
there is no IIO illuminance device. Everything needed (enable ALS, read the
16-bit count) is two registers, and /dev/i2c-0 is already readable by the
`ubuntu` user (i2c group). Mounting a new kernel/DT is not worth the brick risk
on a device without a working USB port.

The count -> lux factor (0.024 lx/count at 50 ms integration) comes from
drivers/iio/light/vcnl4000.c (VCNL4200 ulux_step = 24000). The sensor sits
behind the dark front glass, which attenuates visible light, so the result can
be scaled with VCNL4200_ALS_GAIN (default 1.0) to match a reference lux meter.
"""

import logging
import os
import signal
import statistics
import sys
import time
from pathlib import Path

# Configuration
I2C_BUS = 0
I2C_ADDR = 0x51
REG_ALS_CONF = 0x00
REG_ALS_DATA = 0x09
REG_DEV_ID = 0x0E
PROD_ID_VCNL4200 = 0x58

ALS_SD = 1 << 0            # 1 = ALS shut down
ALS_IT_MASK = 0b11 << 6    # integration time, bits 7:6
ALS_IT_50MS = 0b00 << 6    # index 0 in vcnl4200_als_it_times

LUX_PER_COUNT = 0.024      # 24000 u-lux/count at 50 ms, from the kernel driver
GAIN = float(os.environ.get("VCNL4200_ALS_GAIN", "1.0"))

SAMPLE_INTERVAL = 2.0      # seconds between readings
MEDIAN_WINDOW = 5          # smooth out screen/room flicker
LOG_INTERVAL = 300.0       # log at most this often (the journal is size-capped)
LOG_CHANGE = 0.25          # ...or when lux moves by more than this fraction
ALS_PATH = Path("/tmp/als_lux")

# stdout goes to the journal (persistent, size-capped); a log file of its own grew without bound
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


class AlsSensor:
    def __init__(self):
        from smbus2 import SMBus
        self.bus = SMBus(I2C_BUS)

    def read_id(self):
        """Low byte of the device id register: 0x58 on VCNL4200."""
        return self.bus.read_word_data(I2C_ADDR, REG_DEV_ID) & 0xFF

    def read_conf(self):
        return self.bus.read_word_data(I2C_ADDR, REG_ALS_CONF)

    def enable(self):
        """Clear ALS_SD and select 50 ms integration, leaving other bits alone."""
        conf = self.read_conf()
        new = (conf & ~(ALS_SD | ALS_IT_MASK)) | ALS_IT_50MS
        if new != conf:
            self.bus.write_word_data(I2C_ADDR, REG_ALS_CONF, new)
        return conf, self.read_conf()

    def read_raw(self):
        return self.bus.read_word_data(I2C_ADDR, REG_ALS_DATA)

    def close(self):
        try:
            self.bus.close()
        except Exception:
            pass


def publish(lux):
    """Replace the file atomically: idle-blanker polls it and must never read half a number."""
    tmp = ALS_PATH.with_name(ALS_PATH.name + ".tmp")
    tmp.write_text(f"{lux:.2f}\n")
    os.replace(tmp, ALS_PATH)


def main():
    once = "--once" in sys.argv[1:]
    running = True

    def stop(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    sensor = AlsSensor()
    product_id = sensor.read_id()
    if product_id != PROD_ID_VCNL4200:
        logger.error(f"unexpected product id 0x{product_id:02x} (want 0x{PROD_ID_VCNL4200:02x}) — is this a VCNL4200?")
        return 1

    before, after = sensor.enable()
    logger.info(f"ALS enabled (config 0x{before:04x} -> 0x{after:04x}), 50 ms IT, gain={GAIN:g}")

    if once:
        time.sleep(0.3)
        raw = sensor.read_raw()
        lux = raw * LUX_PER_COUNT * GAIN
        print(f"raw={raw} lux={lux:.2f} (gain={GAIN:g})")
        return 0

    samples = []
    last_log = 0.0
    last_lux = None
    failures = 0
    try:
        while running:
            started = time.monotonic()
            try:
                raw = sensor.read_raw()
            except OSError as e:
                failures += 1
                # Log the first failure and then one a minute, so a dead bus does not flood the journal
                if failures == 1 or failures % 30 == 0:
                    logger.warning(f"ALS read failed ({failures}x): {e}")
            else:
                failures = 0
                samples.append(raw)
                del samples[:-MEDIAN_WINDOW]
                lux = statistics.median(samples) * LUX_PER_COUNT * GAIN
                publish(lux)

                now = time.monotonic()
                changed = last_lux is None or abs(lux - last_lux) > max(1.0, last_lux * LOG_CHANGE)
                if now - last_log >= LOG_INTERVAL or changed:
                    logger.info(f"ALS: raw={raw} lux={lux:.1f}")
                    last_log, last_lux = now, lux

            time.sleep(max(0.2, SAMPLE_INTERVAL - (time.monotonic() - started)))
    finally:
        sensor.close()
        try:
            ALS_PATH.unlink()
        except FileNotFoundError:
            pass
        logger.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
