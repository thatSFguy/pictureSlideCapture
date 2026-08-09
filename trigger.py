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
`gpiocli`, which auto-detects the version.

Two daemon threads cooperate:

  - a **reader** thread streams edges from one long-lived `gpiomon` in real time
    (never blocked by a capture) and records the **timestamp** of the most recent
    debounced edge as a coalesced "capture requested";
  - a **worker** thread performs captures one at a time.

The 400D emits a **phantom obstruct edge as each capture finishes** — its USB
re-enumeration couples a transient onto the sensor line (confirmed in the logs:
an edge at the exact second a shot completes, which a hands-off operator can't
produce, having no completion signal to react to).

That transient is rejected **by timestamp, not by a blanket settle**: the capture
callback reports the moment the camera finished its USB work, and an edge landing
within `cooldown_s` of that moment is discarded as the phantom. Every other edge
is honoured immediately — including one that arrived *during* a capture, which is
a real slide the operator dropped while the previous frame was still downloading.

This replaces an earlier model that waited 2s after every shot and then threw away
whatever had queued during it. That reliably ate the phantom, but the journal
showed it also eating genuine slides whenever the operator fed faster than the
~8s capture cycle — silent frame loss — and it added ~2s of dead time to every
slide. (An even earlier attempt kept queued drops by level-checking the beam with
`gpioget`, but the reader's `gpiomon` holds the line, so every check failed
"sensor read failed" and dropped the slide.) The capture callback is serialized
by the server behind the same camera lock as the web UI, so a sensor trigger and
a button press can never overlap.

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
    "cooldown_s": 1.0,        # PHANTOM WINDOW (seconds). An edge whose timestamp
                              #  falls within this long of the moment the camera
                              #  finished its USB work is treated as the 400D's
                              #  re-enumeration transient and ignored. Every other
                              #  edge is honoured, including one that landed mid-
                              #  capture. See SensorTrigger._serve.
                              #  Keep it TIGHT: it is also the blind spot right
                              #  after a shot, so a wide window silently eats
                              #  slides from an operator who feeds quickly. The
                              #  `[capture] ... tail=` timings in the journal,
                              #  next to the edge lines, show the real offset if
                              #  this needs tuning on the hardware.
}

# Reader-side contact-bounce debounce (seconds): collapse the several edges of a
# single physical slide drop into one trigger. Kept short — a genuine next slide
# is seconds away, never sub-second.
_READER_BOUNCE = 0.3

# The phantom is emitted when gphoto2 closes the USB session, which is a moment
# *before* the callback returns (metering and bookkeeping follow). The capture
# callback reports the USB-completion time itself, but allow a little slack
# either side of it in case that epoch is reported slightly late.
_PHANTOM_LEAD = 0.5


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


def _log_print(msg) -> None:
    """Default logger — flush so lines reach the systemd journal promptly (a
    plain print() to a pipe is block-buffered, which would hide trigger events
    from the in-app log view until the buffer fills)."""
    print(msg, flush=True)


class SensorTrigger:
    """Background watcher that fires `on_trigger` on the sensor's
    unobstructed->obstructed edge. No-op (never starts threads) unless
    mode == 'gpio'. See the module docstring for the reader/worker split."""

    def __init__(self, settings: dict, on_trigger, log=_log_print):
        self._on_trigger = on_trigger
        self._log = log
        self.mode = str(_cfg(settings, "mode")).lower()
        self.chip = str(_cfg(settings, "gpiochip"))
        try:
            self.line = int(_cfg(settings, "sensor_line"))
            cooldown = max(0.0, float(_cfg(settings, "cooldown_s")))
        except (TypeError, ValueError) as e:
            raise TriggerError(f"bad trigger config: {e}")
        self.active_high = bool(_cfg(settings, "active_high"))
        # unobstructed -> obstructed is a rising edge if obstructed reads HIGH,
        # else a falling edge.
        self.edge = "rising" if self.active_high else "falling"
        self.bias = str(_cfg(settings, "bias"))
        self._bounce = _READER_BOUNCE
        self._phantom = cooldown           # ignore edges this close to USB-done
        self._stop = threading.Event()
        self._req = threading.Event()      # a capture is requested (coalesced)
        self._req_at = 0.0                 # monotonic time of that edge
        self._req_lock = threading.Lock()
        self._usb_done = None              # end of the last capture's USB work
        self._reader: threading.Thread | None = None
        self._worker: threading.Thread | None = None
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
        self._stop.clear()
        self._worker = threading.Thread(target=self._serve, name="sensor-capture",
                                        daemon=True)
        self._worker.start()
        self._reader = threading.Thread(target=self._watch, name="sensor-watch",
                                        daemon=True)
        self._reader.start()
        self._log(f"[trigger] watching {self.describe()}")

    # -- reader: stream edges in real time, request captures ----------------
    def _watch(self) -> None:
        # ONE long-lived gpiomon streaming every edge, read in REAL TIME — the
        # capture runs on the worker thread, so this loop is never blocked and an
        # edge that lands DURING a capture is still seen and queued (not missed).
        # Force line-buffered output with `stdbuf -oL` + readline() so edges
        # aren't lagged by libc / Python read-ahead buffering.
        args = gpiocli.with_sudo(
            gpiocli.mon_cmd(self.chip, self.line, self.edge, self.bias),
            self.chip, self.line, self.bias)
        args = self._line_buffered(args)
        self._log(f"[trigger] watcher cmd: {' '.join(args)}")
        fails = 0
        last = 0.0
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(
                    args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1)
            except OSError as e:
                self._log(f"[trigger] cannot start gpiomon: {e}")
                return
            started = time.monotonic()
            while not self._stop.is_set():
                line = self._proc.stdout.readline()
                if line == "":                         # EOF -> gpiomon exited
                    break
                fails = 0
                now = time.monotonic()
                if now - last < self._bounce:          # contact-bounce debounce
                    continue
                last = now
                self._log("[trigger] edge -> capture requested")
                self._request(now)                     # coalesced: at most 1 pending
            err = ""
            try:
                err = (self._proc.stderr.read() or "").strip()
            except Exception:
                pass
            self._proc.wait()
            if self._stop.is_set():
                break
            if time.monotonic() - started < 1.0:       # near-instant exit = config/tool error
                fails += 1
                if fails >= 3:
                    self._log(f"[trigger] giving up after gpiomon errors: "
                              f"{err or 'unknown'}")
                    return
            self._log(f"[trigger] gpiomon exited ({err or 'no error'}); "
                      "restarting in 1s")
            self._stop.wait(1.0)

    # -- request handoff (reader -> worker) ---------------------------------
    def _request(self, ts: float) -> None:
        """Record an edge, coalesced to the EARLIEST pending timestamp.

        Earliest, not latest, and that distinction is the whole trick: the worker
        empties the slot the moment it is free, so anything already pending is a
        real slide dropped mid-capture. The phantom then arrives a fraction of a
        second later — still inside the capture callback's tail — and if it
        overwrote the slot, the worker would judge the phantom's timestamp,
        discard it, and lose the real slide with it. Keeping the earlier
        timestamp means the real drop is what gets judged, and the phantom is
        simply absorbed."""
        with self._req_lock:
            if not self._req_at or ts < self._req_at:
                self._req_at = ts
        self._req.set()

    def _take(self) -> float | None:
        """Consume the pending request, returning the edge's timestamp."""
        with self._req_lock:
            ts = self._req_at
            self._req_at = 0.0
        self._req.clear()
        return ts or None

    def _is_phantom(self, ts: float) -> bool:
        """True if this edge is the camera's USB re-enumeration transient rather
        than a slide: it landed within the phantom window of the moment the last
        capture finished talking to the camera."""
        done = self._usb_done
        return (done is not None
                and done - _PHANTOM_LEAD <= ts <= done + self._phantom)

    # -- worker: capture one slide at a time --------------------------------
    def _serve(self) -> None:
        while not self._stop.is_set():
            if not self._req.wait(0.2):                # nothing pending
                continue
            if self._stop.is_set():
                break
            ts = self._take()
            if ts is None:
                continue
            # Reject the camera's own re-enumeration transient by WHEN it landed,
            # so a real slide dropped mid-capture still gets shot (it used to be
            # discarded wholesale along with the phantom).
            if self._is_phantom(ts):
                self._log("[trigger] ignoring phantom edge (camera "
                          "re-enumeration)")
                continue
            self._log("[trigger] edge detected -> capturing")
            self._usb_done = self._fire(ts)

    def _fire(self, edge_ts: float) -> float:
        """Run the capture callback; returns the monotonic time the camera
        finished its USB work (the callback reports it, since only the capture
        path knows when gphoto2 let go of the bus). Falls back to 'now'.

        The edge timestamp is passed through so the capture path can report how
        long the SHUTTER took to fire after the sensor tripped — the number that
        decides whether the frame beats a moving pusher. Note this method is
        entered before any camera lock is taken, so that wait is included."""
        try:
            done = self._on_trigger(edge_ts)
        except Exception as e:                         # never kill the worker
            self._log(f"[trigger] capture callback error: {e}")
            return time.monotonic()
        return done if isinstance(done, (int, float)) else time.monotonic()

    @staticmethod
    def _line_buffered(args: list[str]) -> list[str]:
        """Prepend `stdbuf -oL` to the gpiomon program so each edge line is
        flushed immediately (skipped if stdbuf isn't installed)."""
        if shutil.which("stdbuf") is None:
            return args
        try:
            i = args.index("gpiomon")
        except ValueError:
            return args
        return args[:i] + ["stdbuf", "-oL"] + args[i:]

    def stop(self) -> None:
        self._stop.set()
        self._req.set()                    # wake the worker so it exits promptly
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        for t in (self._reader, self._worker):
            if t:
                t.join(timeout=3)


def make_trigger(settings: dict, on_trigger, log=_log_print) -> SensorTrigger:
    """Build (but don't start) the configured trigger. Kept parallel to
    advance.make_advancer for consistency."""
    return SensorTrigger(settings, on_trigger, log=log)
