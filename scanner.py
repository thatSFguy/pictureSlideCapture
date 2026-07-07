#!/usr/bin/env python3
"""Film-scanning gantry host controller (dead-reckoning capture loop).

Layer 2 of the architecture in CLAUDE.md: the "brain" that drives the gantry
(GRBL over serial) and the camera (gphoto2 / Canon EOS 400D) and tracks state.

This version is runnable NOW, before the gantry exists: pass no --port and it
uses GrblStub, which just logs the moves it would make while real captures
still happen. That lets the whole capture loop be exercised against the camera.

Key hardware facts this script encodes (all confirmed on real hardware, see
CLAUDE.md):
  - Remote camera settings only apply with the mode dial on M.
  - capturetarget must be Memory card; sdram is unreliable over USB/IP.
  - The USB/IP link periodically re-enumerates; the op right after fails with an
    I/O error, so every gphoto2 op is retried. Running gphoto2 as a fresh
    subprocess per op (rather than holding one session) means each retry
    re-detects the possibly-renumbered device for free.

Example (camera only, stubbed gantry, 2 strips of 6 frames):
    python3 scanner.py --num-strips 2 --frames-per-strip 6 --out-dir ./scans
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from camera import Camera, CameraError


# Baseline scanning settings (applied only with the dial on M, see CLAUDE.md).
DEFAULT_CAM_SETTINGS = {
    "capturetarget": "Memory card",  # NOT sdram — unreliable over USB/IP
    "imageformat": "RAW",
    "iso": "100",
    "aperture": "8",
    # "shutterspeed": "1/60",        # set once exposure is dialed in on the pad
    # "whitebalance": "Manual",
}


# --------------------------------------------------------------------------
# Gantry (GRBL over serial) — stub + real skeleton
# --------------------------------------------------------------------------

class GrblStub:
    """No hardware: logs the moves it would make so the loop is testable now."""

    def __init__(self, verbose: bool = True):
        self.verbose = verbose
        self.pos = (0.0, 0.0)

    def home(self) -> None:
        if self.verbose:
            print("  [gantry:stub] home ($H)")
        self.pos = (0.0, 0.0)

    def move_to(self, x: float, y: float) -> None:
        if self.verbose:
            print(f"  [gantry:stub] G0 X{x:.2f} Y{y:.2f}")
        self.pos = (x, y)

    def wait_idle(self) -> None:
        pass

    def close(self) -> None:
        pass


class GrblSerial:
    """Real GRBL control over a serial port. Requires pyserial.

    Backlash note (CLAUDE.md): always approach a target from the same
    direction. Not yet implemented here — add when the mechanics are known.
    """

    def __init__(self, port: str, baud: int = 115200, verbose: bool = True):
        try:
            import serial  # pyserial, imported lazily so the stub path needs no dep
        except ImportError as e:
            raise CameraError(
                "pyserial not installed — `pip install pyserial` for --port") from e
        self.verbose = verbose
        self.ser = serial.Serial(port, baud, timeout=2)
        time.sleep(2)                    # GRBL boots on port open
        self.ser.reset_input_buffer()
        self._send("\r\n\r\n")           # wake
        time.sleep(1)
        self.ser.reset_input_buffer()

    def _send(self, line: str) -> None:
        self.ser.write(line.encode())

    def _cmd(self, gcode: str) -> None:
        if self.verbose:
            print(f"  [gantry] {gcode}")
        self._send(gcode + "\n")
        # GRBL replies "ok" or "error:N" per line
        resp = self.ser.readline().decode(errors="replace").strip()
        if resp.startswith("error"):
            raise CameraError(f"GRBL rejected '{gcode}': {resp}")

    def home(self) -> None:
        self._cmd("$H")

    def move_to(self, x: float, y: float) -> None:
        self._cmd(f"G0 X{x:.3f} Y{y:.3f}")

    def wait_idle(self, poll: float = 0.1) -> None:
        while True:
            self._send("?")
            status = self.ser.readline().decode(errors="replace")
            if "Idle" in status:
                return
            time.sleep(poll)

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Resumable state
# --------------------------------------------------------------------------

@dataclass
class ScanState:
    path: Path
    done: list[str] = field(default_factory=list)   # e.g. "strip03_frame05"

    @classmethod
    def load(cls, path: Path) -> "ScanState":
        if path.exists():
            data = json.loads(path.read_text())
            return cls(path=path, done=data.get("done", []))
        return cls(path=path)

    def is_done(self, key: str) -> bool:
        return key in self.done

    def mark(self, key: str) -> None:
        self.done.append(key)
        self.path.write_text(json.dumps(
            {"done": self.done}, indent=2))


# --------------------------------------------------------------------------
# Main scan loop
# --------------------------------------------------------------------------

def run_scan(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    state = ScanState.load(out_dir / "scan_state.json")

    cam = Camera(retries=args.retries, verbose=True)
    gantry = (GrblSerial(args.port, verbose=True) if args.port
              else GrblStub(verbose=True))

    print(f"Detecting camera...\n  {cam.detect()}")
    if not args.no_check:
        cam.assert_manual_mode()
        batt = cam.battery()
        print(f"Battery: {batt}")
        if batt.lower() == "low":
            print("  WARNING: battery Low — use AC adapter for batch runs.")
    print("Applying camera settings...")
    cam.configure(DEFAULT_CAM_SETTINGS)

    print("Homing gantry...")
    gantry.home()

    total = args.num_strips * args.frames_per_strip
    shot = 0
    for strip in range(args.num_strips):
        for frame in range(args.frames_per_strip):
            shot += 1
            key = f"strip{strip:02d}_frame{frame:02d}"
            if state.is_done(key):
                print(f"[{shot}/{total}] {key} already done, skipping")
                continue

            x = args.origin_x + frame * args.frame_pitch
            y = args.origin_y + strip * args.strip_pitch
            print(f"[{shot}/{total}] {key} -> X{x:.2f} Y{y:.2f}")

            gantry.move_to(x, y)
            gantry.wait_idle()
            time.sleep(args.settle)          # vibration settle (CLAUDE.md)

            dest = out_dir / f"{key}.cr2"
            try:
                cam.capture(dest)
            except CameraError as e:
                print(f"  ERROR capturing {key}: {e}")
                if args.stop_on_error:
                    return 1
                continue
            state.mark(key)
            print(f"  saved {dest} ({dest.stat().st_size} bytes)")

    gantry.close()
    print(f"\nDone. {len(state.done)}/{total} frames captured. "
          f"State: {state.path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    grid = p.add_argument_group("grid geometry (mm)")
    grid.add_argument("--origin-x", type=float, default=0.0)
    grid.add_argument("--origin-y", type=float, default=0.0)
    grid.add_argument("--frame-pitch", type=float, default=38.0,
                      help="center-to-center along a strip (36mm frame + 2mm gap)")
    grid.add_argument("--strip-pitch", type=float, default=10.0,
                      help="center-to-center between strips (set to your holder)")
    grid.add_argument("--num-strips", type=int, required=True)
    grid.add_argument("--frames-per-strip", type=int, required=True)

    io = p.add_argument_group("io / behavior")
    io.add_argument("--out-dir", default="./scans")
    io.add_argument("--port", default=None,
                    help="GRBL serial port (e.g. /dev/ttyUSB0). Omit for stub.")
    io.add_argument("--settle", type=float, default=0.5,
                    help="settle delay (s) after motion before shutter")
    io.add_argument("--retries", type=int, default=4,
                    help="gphoto2 retry count for USB/IP re-enum errors")
    io.add_argument("--stop-on-error", action="store_true")
    io.add_argument("--no-check", action="store_true",
                    help="skip mode-dial/battery preflight checks")
    return p


if __name__ == "__main__":
    try:
        sys.exit(run_scan(build_parser().parse_args()))
    except KeyboardInterrupt:
        print("\nInterrupted — progress saved to scan_state.json; rerun to resume.")
        sys.exit(130)
    except CameraError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
