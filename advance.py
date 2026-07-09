#!/usr/bin/env python3
"""Auto slide-advance output (STUB — bring-up required).

Turns the capture loop into: capture -> advance one slide -> capture -> ...
so a tray/carousel can be digitized without touching it between frames. Which
mechanism drives the advance is SETTINGS-DRIVEN (see ADVANCE_DEFAULTS); the
default is "off" (NullAdvancer, a no-op), so nothing moves until hardware is
wired and the mode is set.

Two planned mechanisms:
  - "motor"   : a plain DC gearmotor run until a stop/index switch trips.
                One switch pulse == one slide. A run TIMEOUT is mandatory so a
                missed switch can't run the motor forever (jam protection).
  - "stepper" : a stepper advanced a fixed number of steps per slide (STEP/DIR
                pins), or a relative move handed to GRBL. Reliable step timing
                wants a real step generator, so the stub points at GRBL.

GPIO is driven by shelling out to libgpiod's `gpioset` / `gpiomon` — the same
subprocess-to-CLI pattern the project uses for gphoto2, which keeps it pip-free
(no RPi.GPIO / gpiozero dependency). Install on the Pi with `apt install
gpiod`. The motor path below is written but UNTESTED on hardware; treat the
first run as a shakedown. The stepper path is intentionally left unimplemented.

Nothing here is invoked unless `mode` is set to "motor"/"stepper" in settings,
so importing/using this module with the default config is a safe no-op.
"""

from __future__ import annotations

import shutil
import subprocess
import time

# ---- settings schema (all keys optional; missing ones fall back here) ------
ADVANCE_DEFAULTS = {
    "mode": "off",            # "off" | "motor" | "stepper"
    "after_capture": True,    # advance automatically after each real capture

    # --- shared GPIO ---
    "gpiochip": "gpiochip0",  # libgpiod chip name

    # --- motor + stop-switch ---
    "motor_line": 17,         # BCM line driving the motor (via a driver/relay)
    "motor_active_high": True,
    "switch_line": 27,        # stop/index switch input line
    "switch_falling": True,   # True: trip on falling edge (switch pulls low)
    "timeout_s": 4.0,         # abort + de-energize if the switch never trips
    "settle_ms": 150,         # pause after the switch trips before returning

    # --- stepper (STEP/DIR) or GRBL ---
    "step_line": 17,
    "dir_line": 18,
    "enable_line": 22,
    "steps_per_slide": 400,
    "direction": 1,           # +1 / -1
    "step_hz": 800,           # step pulse rate
}


class AdvanceError(Exception):
    """Advance failed (jam / timeout / missing tools / not implemented)."""


def _cfg(settings: dict, key: str):
    return settings.get(key, ADVANCE_DEFAULTS[key])


class Advancer:
    """Base output. Subclasses implement advance(). enabled gates the loop."""

    enabled = False
    mode = "off"

    def advance(self) -> None:
        """Advance exactly one slide, or raise AdvanceError."""
        raise NotImplementedError

    def describe(self) -> str:
        return self.mode


class NullAdvancer(Advancer):
    """Default: does nothing. Used when mode is 'off' or unrecognized."""

    def advance(self) -> None:
        return


class MotorAdvancer(Advancer):
    """DC motor run until a stop/index switch trips (one pulse == one slide).

    UNTESTED on hardware. Requires libgpiod (`gpioset`, `gpiomon`).
    """

    enabled = True
    mode = "motor"

    def __init__(self, s: dict):
        try:
            self.chip = str(_cfg(s, "gpiochip"))
            self.motor_line = int(_cfg(s, "motor_line"))
            self.motor_on = "1" if _cfg(s, "motor_active_high") else "0"
            self.motor_off = "0" if _cfg(s, "motor_active_high") else "1"
            self.switch_line = int(_cfg(s, "switch_line"))
            self.switch_edge = "--falling-edge" if _cfg(s, "switch_falling") \
                else "--rising-edge"
            self.timeout_s = float(_cfg(s, "timeout_s"))
            self.settle_ms = int(_cfg(s, "settle_ms"))
        except (TypeError, ValueError) as e:
            raise AdvanceError(f"bad motor advance config: {e}")

    def _require_tools(self) -> None:
        # Checked at advance() time, not construction, so the mode can be
        # selected/configured from any machine (e.g. a laptop) even without
        # libgpiod installed. Only actually moving the motor needs the tools.
        for tool in ("gpioset", "gpiomon"):
            if shutil.which(tool) is None:
                raise AdvanceError(f"{tool} not found — `apt install gpiod`")

    def _set_motor(self, value: str) -> None:
        # gpioset holds the line only while it runs; for a momentary drive we
        # start it, wait for the switch, then kill it. Here we use the
        # fire-and-return form to set a steady level via a backgrounded hold.
        subprocess.run(["gpioset", self.chip, f"{self.motor_line}={value}"],
                       check=True, timeout=5)

    def advance(self) -> None:
        self._require_tools()
        # Energize, wait for the switch edge (bounded by timeout), de-energize.
        hold = subprocess.Popen(
            ["gpioset", "--mode=signal", self.chip,
             f"{self.motor_line}={self.motor_on}"])
        try:
            r = subprocess.run(
                ["gpiomon", "--num-events=1", self.switch_edge,
                 self.chip, str(self.switch_line)],
                timeout=self.timeout_s)
            if r.returncode != 0:
                raise AdvanceError("switch never tripped (motor/switch wiring?)")
        except subprocess.TimeoutExpired:
            raise AdvanceError(
                f"advance timed out after {self.timeout_s:.1f}s — jam or "
                "missed index switch")
        finally:
            hold.terminate()
            try:
                hold.wait(timeout=2)
            except subprocess.TimeoutExpired:
                hold.kill()
            self._set_motor(self.motor_off)  # ensure de-energized
        if self.settle_ms:
            time.sleep(self.settle_ms / 1000.0)


class StepperAdvancer(Advancer):
    """Fixed steps-per-slide stepper. NOT IMPLEMENTED.

    Reliable step timing is hard to bit-bang from userspace via gpioset. The
    intended path is to hand a relative move to GRBL (reuse GrblStub/serial
    from scanner.py) or a dedicated step generator, rather than pulsing GPIO in
    a Python loop. Left as a stub until the hardware choice is final.
    """

    enabled = True
    mode = "stepper"

    def __init__(self, s: dict):
        try:
            self.steps = int(_cfg(s, "steps_per_slide"))
            self.direction = int(_cfg(s, "direction"))
        except (TypeError, ValueError) as e:
            raise AdvanceError(f"bad stepper advance config: {e}")

    def advance(self) -> None:
        raise AdvanceError(
            "stepper advance not implemented — wire to GRBL relative move "
            "(see scanner.py) or a step generator")


def make_advancer(settings: dict) -> Advancer:
    """Build the configured advancer. Unknown/off -> NullAdvancer (no-op)."""
    mode = str(_cfg(settings, "mode")).lower()
    try:
        if mode == "motor":
            return MotorAdvancer(settings)
        if mode == "stepper":
            return StepperAdvancer(settings)
    except AdvanceError:
        raise
    return NullAdvancer()
