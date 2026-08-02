#!/usr/bin/env python3
"""Optical-sensor capture trigger (settings-driven; default off).

Watches a digital sensor's OUT line and fires a capture on the
**unobstructed -> obstructed** transition, so a slide dropping into position (or
any object breaking the beam) takes the photo without a button press.

Which *electrical* edge "obstructed" is depends on the sensor module's output
polarity, and that varies part-to-part and often isn't known up front — so it's
a setting (`active_high`):

  - active_high = False  (DEFAULT): OUT idles HIGH and pulls LOW when the beam is
                          obstructed. This is the common open-collector / most
                          IR break-beam & photo-interrupter behaviour.
                          -> trigger on the FALLING edge.
  - active_high = True :  OUT idles LOW and goes HIGH when obstructed.
                          -> trigger on the RISING edge.

If you don't know which one your sensor is: leave it on the default, block the
beam, and use `read_level()` (Setup -> System -> Read sensor) to see whether the
line reads 0 or 1 when obstructed, then set `active_high` to match.

Like advance.py, GPIO is driven by shelling out to libgpiod (`gpiomon` /
`gpioget`) — the same subprocess-to-CLI pattern the project uses for gphoto2, so
there's no pip dependency (`apt install gpiod`). Command syntax differs between
libgpiod v1 and v2 (recent Raspberry Pi OS ships v2), so the argv is built by
`gpiocli`, which auto-detects the version. The watcher runs in a daemon
thread reading one long-lived `gpiomon` (so no edges are missed between polls);
each detected edge calls the capture callback, which the server serializes
behind the same camera lock as the web UI, so a sensor trigger and a button
press can never overlap.

Nothing here runs unless `mode` is "gpio" in settings, so importing/using this
module with the default config is a safe no-op (and needs no GPIO hardware).
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time

import gpiocli

# ---- settings schema (all keys optional; missing ones fall back here) ------
TRIGGER_DEFAULTS = {
    "mode": "off",            # "off" | "gpio"
    "gpiochip": "gpiochip0",  # libgpiod chip name
    "sensor_line": 24,        # BCM line wired to the sensor OUT (phys pin 18)
    "active_high": False,     # False: OUT pulls LOW when obstructed (typical) ->
                              #        falling edge. True: HIGH -> rising edge.
    "bias": "pull-up",        # internal bias: pull-up|pull-down|disable|as-is
                              # (pull-up gives an open-collector sensor a defined
                              #  idle HIGH; use as-is for a push-pull output)
    "cooldown_s": 1.5,        # ignore new edges this long after one fires
                              # (software debounce + no double-fire during the
                              #  capture's own download)
}


class TriggerError(Exception):
    """Sensor trigger could not be configured or started (bad config / missing
    tools / GPIO error)."""


def _cfg(s: dict, k: str):
    return s.get(k, TRIGGER_DEFAULTS[k])


def read_level(settings: dict) -> int:
    """Read the sensor's current raw level (0 or 1) with `gpioget`.

    Handy for figuring out polarity without any capture: block the beam, read,
    and note whether it's 0 or 1 — that tells you whether the sensor is
    active-low (obstructed == 0) or active-high (obstructed == 1). Raises
    TriggerError if gpioget is missing or the read fails."""
    if shutil.which("gpioget") is None:
        raise TriggerError("gpioget not found — run `sudo apt install gpiod`")
    chip = str(_cfg(settings, "gpiochip"))
    try:
        line = int(_cfg(settings, "sensor_line"))
    except (TypeError, ValueError) as e:
        raise TriggerError(f"bad sensor_line: {e}")
    bias = _cfg(settings, "bias")
    args = gpiocli.with_sudo(gpiocli.get_cmd(chip, line, bias), chip, line, bias)
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TriggerError(str(e))
    if r.returncode != 0:
        raise TriggerError((r.stderr or r.stdout or "gpioget failed").strip())
    try:
        return gpiocli.parse_level(r.stdout)
    except ValueError:
        raise TriggerError(f"unexpected gpioget output: {r.stdout!r}")


class SensorTrigger:
    """Background watcher that fires `on_trigger` on the sensor's
    unobstructed->obstructed edge. No-op (never starts a thread) unless
    mode == 'gpio'."""

    def __init__(self, settings: dict, on_trigger, log=print):
        self._on_trigger = on_trigger
        self._log = log
        self.mode = str(_cfg(settings, "mode")).lower()
        self.chip = str(_cfg(settings, "gpiochip"))
        try:
            self.line = int(_cfg(settings, "sensor_line"))
            self.cooldown = max(0.0, float(_cfg(settings, "cooldown_s")))
        except (TypeError, ValueError) as e:
            raise TriggerError(f"bad trigger config: {e}")
        self.active_high = bool(_cfg(settings, "active_high"))
        # unobstructed -> obstructed is a rising edge if obstructed reads HIGH,
        # else a falling edge.
        self.edge = "rising" if self.active_high else "falling"
        self.bias = str(_cfg(settings, "bias"))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None

    @property
    def enabled(self) -> bool:
        return self.mode == "gpio"

    def describe(self) -> str:
        if not self.enabled:
            return "off"
        pol = "active-high (rising)" if self.active_high else "active-low (falling)"
        return f"GPIO{self.line} {pol}, chip {self.chip}"

    def start(self) -> None:
        """Start watching (raises TriggerError if enabled but gpiomon absent).
        No-op when disabled."""
        if not self.enabled:
            return
        if shutil.which("gpiomon") is None:
            raise TriggerError("gpiomon not found — run `sudo apt install gpiod`")
        self._thread = threading.Thread(target=self._run, name="sensor-trigger",
                                        daemon=True)
        self._thread.start()
        self._log(f"[trigger] watching {self.describe()}")

    def _run(self) -> None:
        # One gpiomon per event (--num-events 1): the process exits the instant
        # the edge fires, which flushes its output and returns to us immediately.
        # A long-lived streaming gpiomon block-buffers its pipe (and Python adds
        # read-ahead), so edges arrived seconds late — the capture lagged the
        # beam break. The only gap this introduces is between one exit and the
        # next start (~tens of ms), which lands during the post-capture cooldown
        # when no new slide is due, so no real edge is missed.
        args = gpiocli.with_sudo(
            gpiocli.mon_cmd(self.chip, self.line, self.edge, self.bias,
                            num_events=1),
            self.chip, self.line, self.bias)
        fails = 0
        last = 0.0
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(
                    args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    text=True)
            except OSError as e:
                self._log(f"[trigger] cannot start gpiomon: {e}")
                return
            _out, err = self._proc.communicate()       # blocks until the 1 edge
            rc = self._proc.returncode
            if self._stop.is_set():                    # terminated by stop()
                break
            if rc != 0:                                # error, not an edge
                fails += 1
                msg = (err or "").strip() or f"exit {rc}"
                if fails >= 3:
                    self._log(f"[trigger] giving up after gpiomon errors: {msg}")
                    return
                self._log(f"[trigger] gpiomon error ({msg}); retrying in 1s")
                self._stop.wait(1.0)
                continue
            fails = 0
            now = time.monotonic()
            if now - last < self.cooldown:             # debounce / ignore bounce
                continue
            last = now
            try:
                self._on_trigger()
            except Exception as e:                     # never kill the watcher
                self._log(f"[trigger] capture callback error: {e}")
            last = time.monotonic()                    # re-arm cooldown post-capture

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)


def make_trigger(settings: dict, on_trigger, log=print) -> SensorTrigger:
    """Build (but don't start) the configured trigger. Kept parallel to
    advance.make_advancer for consistency."""
    return SensorTrigger(settings, on_trigger, log=log)
