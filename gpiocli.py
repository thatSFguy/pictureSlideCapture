#!/usr/bin/env python3
"""Version-aware libgpiod CLI command builders (v1 and v2).

Raspberry Pi OS shipped libgpiod v1 (Bullseye / early Bookworm) and later v2
(recent Bookworm), and the gpioget / gpiomon / gpioset command-line syntax is
INCOMPATIBLE between them:

    read a line :  v1   gpioget <chip> <line>
                   v2   gpioget -c <chip> <line>
    watch edges :  v1   gpiomon --rising-edge <chip> <line>
                   v2   gpiomon -e rising -c <chip> <line>

So a hard-coded command works on one and fails on the other. On v2 the v1 form
fails with "cannot find line 'gpiochip0'" because v2 parses the chip name as a
line name. These builders detect the installed major version once and emit the
right argv, so advance.py and trigger.py run on both. Reads parse either the v1
("0"/"1") or v2 ("<line>=active|inactive") output format.

Same subprocess-to-CLI pattern as the rest of the project (no pip / RPi.GPIO
dependency) — needs `apt install gpiod`.
"""

from __future__ import annotations

import functools
import re
import shutil
import subprocess

_BIAS_OK = ("pull-up", "pull-down", "disable", "as-is")


@functools.lru_cache(maxsize=1)
def major() -> int:
    """Installed libgpiod CLI major version (1 or 2). Defaults to 2 (what recent
    Raspberry Pi OS ships) when it can't be determined."""
    for tool in ("gpiodetect", "gpioget", "gpiomon"):
        if not shutil.which(tool):
            continue
        try:
            r = subprocess.run([tool, "--version"], capture_output=True,
                               text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        m = re.search(r"(\d+)\.\d+", (r.stdout or "") + " " + (r.stderr or ""))
        if m:
            return int(m.group(1))
    return 2


def _bias_args(bias) -> list[str]:
    """`--bias` is the long option on both v1 and v2; only add it for a real
    value (as-is / unrecognized -> leave the line's existing bias)."""
    b = str(bias).lower()
    return ["--bias", b] if b in _BIAS_OK and b != "as-is" else []


def get_cmd(chip, line, bias="as-is") -> list[str]:
    """argv to read one line's level."""
    base = ["gpioget", *_bias_args(bias)]
    if major() >= 2:
        return [*base, "-c", str(chip), str(line)]
    return [*base, str(chip), str(line)]


def parse_level(out: str) -> int:
    """gpioget output -> 0/1. Handles v1 ('0'/'1') and v2 ('24=inactive')."""
    low = (out or "").strip().lower()
    if not low:
        raise ValueError("empty gpioget output")
    if "inactive" in low:                 # check before 'active' (substring)
        return 0
    if "active" in low:
        return 1
    return int(low.split()[-1])           # v1 numeric


def mon_cmd(chip, line, edge, bias="as-is", num_events=None) -> list[str]:
    """argv to monitor edges. edge is 'rising' | 'falling' | 'both'. Omit
    num_events for a continuous stream (one line printed per event)."""
    base = ["gpiomon", *_bias_args(bias)]
    if num_events:
        base += ["--num-events", str(num_events)]     # long form on v1 and v2
    if major() >= 2:
        return [*base, "-e", edge, "-c", str(chip), str(line)]
    # v1: rising/falling via flags; 'both' -> omit (v1 default watches both)
    if edge in ("rising", "falling"):
        base += [f"--{edge}-edge"]
    return [*base, str(chip), str(line)]


def set_hold_cmd(chip, line, value) -> list[str]:
    """argv to drive `line` to `value` and HOLD it until the process is killed
    (v1 needs --mode=signal; v2 blocks by default). Run via Popen and
    terminate() to release — the line reverts to its default on release."""
    if major() >= 2:
        return ["gpioset", "-c", str(chip), f"{line}={value}"]
    return ["gpioset", "--mode=signal", str(chip), f"{line}={value}"]
