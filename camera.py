#!/usr/bin/env python3
"""Camera control for the Canon EOS 400D via the gphoto2 CLI.

Shared by scanner.py (gantry batch loop) and capture_server.py (web UI).

Design notes (confirmed on hardware — see CLAUDE.md):
  - One gphoto2 subprocess per operation. A fresh process re-detects the device
    for free, which is what makes the USB re-enumeration retry work.
  - Remote settings only apply with the mode dial on M.
  - capturetarget must be Memory card; sdram is unreliable.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path


# Substrings that indicate the transient USB re-enumeration / claim error
# (seen under WSL USB/IP; harmless on a Pi but retried anyway).
_RETRYABLE = ("i/o problem", "-7", "could not find the requested device",
              "could not claim", "no camera found", "-53", "-52")


class CameraError(RuntimeError):
    pass


class Camera:
    """Controls the DSLR via the gphoto2 CLI, one subprocess per operation."""

    def __init__(self, retries: int = 4, backoff: float = 1.5,
                 verbose: bool = True):
        self.retries = retries
        self.backoff = backoff
        self.verbose = verbose
        self.last_stdout = ""     # gphoto2 output of the most recent success

    def _run(self, args: list[str], timeout: float = 60.0) -> str:
        """Run one gphoto2 command with retry-on-IO-error + backoff."""
        last = ""
        for attempt in range(1, self.retries + 1):
            try:
                proc = subprocess.run(
                    ["gphoto2", *args],
                    capture_output=True, text=True, timeout=timeout,
                )
            except FileNotFoundError as e:
                raise CameraError("gphoto2 not installed on this host") from e
            except subprocess.TimeoutExpired:
                last = f"timeout after {timeout}s"
            else:
                if proc.returncode == 0:
                    self.last_stdout = proc.stdout
                    return proc.stdout
                last = (proc.stderr or proc.stdout).strip()

            retryable = any(s in last.lower() for s in _RETRYABLE)
            if attempt < self.retries and retryable:
                wait = self.backoff * attempt
                if self.verbose:
                    first = last.splitlines()[0] if last else "?"
                    print(f"  [camera] transient error (attempt {attempt}), "
                          f"re-detecting in {wait:.1f}s: {first}")
                time.sleep(wait)
                subprocess.run(["gphoto2", "--auto-detect"],
                               capture_output=True, text=True)
                continue
            break
        raise CameraError(f"gphoto2 {' '.join(args)} failed: {last}")

    # -- queries -----------------------------------------------------------

    def detect(self) -> str:
        out = self._run(["--auto-detect"])
        if "usb:" not in out:
            raise CameraError("no camera detected on USB")
        return out.strip()

    def model(self) -> str:
        for line in self._run(["--auto-detect"]).splitlines():
            if "usb:" in line:
                return line.rsplit("usb:", 1)[0].strip()
        return "unknown"

    def get_config(self, name: str) -> str:
        out = self._run(["--get-config", name])
        for line in out.splitlines():
            if line.startswith("Current:"):
                return line.split(":", 1)[1].strip()
        return ""

    def get_many(self, names: list[str]) -> dict[str, str]:
        """Fetch several Current values in ONE gphoto2 call (positional parse)."""
        args: list[str] = []
        for n in names:
            args += ["--get-config", n]
        out = self._run(args)
        currents = [l.split(":", 1)[1].strip()
                    for l in out.splitlines() if l.startswith("Current:")]
        return dict(zip(names, currents))

    def get_config_full(self, names: list[str]) -> dict[str, dict]:
        """One gphoto2 call -> {name: {"current": str, "choices": [str,...]}}.
        Blocks come back in request order (delimited by 'Label:')."""
        args: list[str] = []
        for n in names:
            args += ["--get-config", n]
        out = self._run(args)
        blocks: list[dict] = []
        cur: dict | None = None
        for line in out.splitlines():
            if line.startswith("Label:"):
                if cur is not None:
                    blocks.append(cur)
                cur = {"current": "", "choices": []}
            elif cur is not None:
                if line.startswith("Current:"):
                    cur["current"] = line.split(":", 1)[1].strip()
                elif line.startswith("Choice:"):
                    parts = line.split(None, 2)
                    if len(parts) == 3:
                        cur["choices"].append(parts[2])
        if cur is not None:
            blocks.append(cur)
        return {n: blocks[i] for i, n in enumerate(names) if i < len(blocks)}

    def config_choices(self, name: str) -> list[str]:
        out = self._run(["--get-config", name])
        choices = []
        for line in out.splitlines():
            if line.startswith("Choice:"):
                # "Choice: 6 RAW" -> "RAW"
                parts = line.split(None, 2)
                if len(parts) == 3:
                    choices.append(parts[2])
        return choices

    def mode(self) -> str:
        return self.get_config("autoexposuremode")

    def battery(self) -> str:
        return self.get_config("batterylevel")

    def available_shots(self) -> str:
        return self.get_config("availableshots")

    def is_manual(self) -> bool:
        return self.mode().lower() == "manual"

    def assert_manual_mode(self) -> None:
        m = self.mode()
        if m.lower() != "manual":
            raise CameraError(
                f"mode dial is on '{m}', not Manual — remote settings will not "
                "apply. Turn the physical dial to M.")

    # -- actions -----------------------------------------------------------

    def configure(self, settings: dict[str, str]) -> None:
        args: list[str] = []
        for k, v in settings.items():
            args += ["--set-config", f"{k}={v}"]
        if args:
            self._run(args)

    def capture(self, dest: Path, capturetarget: str = "Memory card") -> str:
        """Trigger, download to `dest` (may create sibling files for RAW+JPEG),
        delete from card. `dest` may use gphoto2's %C extension token.
        Returns the gphoto2 stdout (useful for diagnosing an empty download).

        capturetarget is set in the SAME gphoto2 invocation as the capture, on
        purpose: setting it in a separate command re-enumerates the 400D and
        resets it back to Internal RAM, so the next (separate) capture shoots to
        RAM and downloads nothing. One session avoids that reset; on a retry the
        set is a no-op (already correct) so it can't loop. Pass capturetarget=""
        to skip setting it."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        args: list[str] = []
        if capturetarget:
            args += ["--set-config-value", f"capturetarget={capturetarget}"]
        args += ["--capture-image-and-download", "--filename", str(dest),
                 "--force-overwrite"]
        return self._run(args, timeout=90.0)
