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

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

# The persistent session's --filename is fixed when it launches, so shell
# captures all land on this stem and are renamed onto the real one afterwards.
# Leading underscore keeps it out of the group globs, which match "<prefix>_NNNN".
SHELL_CAPTURE_STEM = "_gpshell"


# Substrings that indicate a transient USB re-enumeration / claim / device-busy
# error worth retrying. The 400D drops off and re-enumerates after every SDRAM
# capture, so a command fired in that window (a reshoot, an auto-expose step, a
# rapid sensor shot) comes back with one of these. "busy" / PTP 0x2019 were the
# gap: gphoto2 reports the re-enumeration as "PTP Device Busy" (device-busy),
# which the old list missed, so those ops failed instead of retrying. The camera
# is single-session behind our lock, so "busy" is always transient here.
_RETRYABLE = ("i/o problem", "-7", "could not find the requested device",
              "could not claim", "no camera found", "-53", "-52",
              "busy", "0x2019", "-110", "-16")


class CameraError(RuntimeError):
    pass


# ---- persistent gphoto2 shell session -------------------------------------
# Spawning a gphoto2 process per operation costs ~3.1s on a Pi Zero W before the
# shutter even moves (measured: process start + libgphoto2 camlib scan + USB
# claim + PTP OpenSession). On a rig whose slide pusher keeps rotating, that is
# the throughput limit. `gphoto2 --shell` pays it ONCE and then takes commands on
# stdin with the PTP session already open.
#
# Synchronising with it needs care. The shell's prompt goes through readline,
# which may not print it at all when stdin is a pipe, so the prompt is NOT a safe
# end-of-output marker. Instead each command is followed by a unique BOGUS
# command; the shell answers with "Command '<marker>' not found." and carries on.
# That string is emitted unconditionally, so it is a reliable sentinel.
_SHELL_SENTINEL = "Command '{}' not found"

# Consecutive shell failures before giving up on it for the rest of the run.
# Falling back per-capture is fine; silently retrying a broken session forever
# would be SLOWER than never having tried (a doomed attempt plus the legacy run).
_SHELL_MAX_FAILS = 3


class ShellSession:
    """A long-lived `gphoto2 --shell` holding the camera's PTP session open.

    Only one process can claim the camera at a time, so while this is alive
    every camera operation must go through it (Camera._run enforces that by
    closing the session before any command it cannot translate)."""

    def __init__(self, cwd: str, filename: str, log=None):
        self.cwd = cwd
        self.filename = filename
        self._log = log or (lambda m: None)
        self._proc: subprocess.Popen | None = None
        self._buf = ""
        self._cond = threading.Condition()
        self._eof = False
        self._seq = 0
        self._reader: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------------
    def start(self, timeout: float = 12.0) -> None:
        """Spawn the shell and block until it answers, i.e. the camera is
        claimed and the PTP session is open. Raises CameraError if it won't."""
        argv = ["gphoto2", "--filename", self.filename, "--force-overwrite",
                "--shell"]
        # Force line buffering. gphoto2 writes with stdio, which block-buffers
        # when stdout is a pipe, so WITHOUT this its replies — including the
        # sentinel we synchronise on — sit in libc's buffer and never arrive.
        # v0.1.35 shipped without it and the camera stopped taking pictures
        # entirely: every session start blocked for its full timeout, and with
        # the sensor trigger's lock wait on top, every slide was skipped as
        # "camera busy". The test stub flushed explicitly, so the suite passed
        # against a fiction. trigger.py already does exactly this for gpiomon.
        if shutil.which("stdbuf"):
            argv = ["stdbuf", "-oL", *argv]
        else:
            self._log("[camera] stdbuf not found — a persistent gphoto2 session "
                      "cannot be synchronised reliably; not starting one")
            raise CameraError("stdbuf unavailable (needed for line buffering)")
        try:
            self._proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, cwd=self.cwd, bufsize=0)
        except OSError as e:
            raise CameraError(f"cannot start gphoto2 shell: {e}")
        self._reader = threading.Thread(target=self._pump, daemon=True,
                                        name="gphoto2-shell")
        self._reader.start()
        # A no-op round trip proves the session is up (and swallows the banner).
        self.run([], timeout=timeout)
        self._log("[camera] persistent gphoto2 session open")

    def _pump(self) -> None:
        """Drain stdout into the buffer. Reads raw bytes rather than lines: the
        shell's last output before it waits for input has no trailing newline,
        so a readline() loop would block with data still unseen."""
        fd = self._proc.stdout
        while True:
            try:
                chunk = fd.read(4096)
            except (OSError, ValueError):
                chunk = b""
            if not chunk:
                with self._cond:
                    self._eof = True
                    self._cond.notify_all()
                return
            with self._cond:
                self._buf += chunk.decode("utf-8", "replace")
                self._cond.notify_all()

    def alive(self) -> bool:
        return (self._proc is not None and self._proc.poll() is None
                and not self._eof)

    def close(self) -> None:
        p, self._proc = self._proc, None
        if p is None:
            return
        try:
            if p.poll() is None:
                try:
                    p.stdin.write(b"quit\n")
                    p.stdin.flush()
                except (OSError, ValueError):
                    pass
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=3)
        finally:
            for s in (p.stdin, p.stdout):
                try:
                    s.close()
                except Exception:
                    pass

    # -- command ------------------------------------------------------------
    def run(self, cmds: list[str], timeout: float = 25.0, on_event=None,
            markers: tuple = ()) -> str:
        """Send commands and return everything the shell printed for them.

        `on_event` fires the moment any of `markers` appears in the output —
        used to report the shutter without waiting for the download."""
        if self._proc is None or self._proc.poll() is not None:
            raise CameraError("gphoto2 shell is not running")
        self._seq += 1
        marker = f"__sync{self._seq}__"
        sentinel = _SHELL_SENTINEL.format(marker)
        with self._cond:
            self._buf = ""
        payload = "".join(c + "\n" for c in cmds) + marker + "\n"
        try:
            self._proc.stdin.write(payload.encode())
            self._proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise CameraError(f"gphoto2 shell died: {e}")

        fired, deadline = False, time.monotonic() + timeout
        while True:
            with self._cond:
                buf = self._buf
                if sentinel not in buf:
                    if self._eof:
                        raise CameraError("gphoto2 shell exited unexpectedly")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CameraError(f"gphoto2 shell timeout after {timeout}s")
                    self._cond.wait(min(0.2, remaining))
            if not fired and markers and on_event and \
                    any(m in buf.lower() for m in markers):
                fired = True
                try:
                    on_event()
                except Exception:      # never let a callback break a capture
                    pass
            if sentinel in buf:
                return buf.split(sentinel)[0]


class Camera:
    """Controls the DSLR via the gphoto2 CLI, one subprocess per operation."""

    def __init__(self, retries: int = 4, backoff: float = 1.5,
                 verbose: bool = True, persistent: bool = False):
        self.retries = retries
        self.backoff = backoff
        self.verbose = verbose
        self.last_stdout = ""     # gphoto2 output of the most recent success
        self._target_set = None   # capturetarget already sent this session
        # Persistent session (see ShellSession). Kept optional and self-
        # disabling: it is a large win when it works, but if this camera can't
        # hold a session across captures every attempt is wasted time, so
        # repeated failures switch it off rather than pay that cost forever.
        self.persistent = persistent
        self._shell: ShellSession | None = None
        self._shell_fails = 0
        self._shell_off = False   # gave up on it for this run
        self._last_shell_cwd = None
        self._model_cache = ""
        self.shell_stats = {"shell": 0, "legacy": 0, "fallbacks": 0}

    # -- persistent session management --------------------------------------
    def _shell_usable(self) -> bool:
        return self.persistent and not self._shell_off

    def _ensure_shell(self, cwd: str | None) -> ShellSession | None:
        """Return a live session, starting one if needed. Returns None (and
        disables the feature after repeated failures) if it can't be established.

        `cwd` is where downloads must land, and only a capture cares: gphoto2
        stages downloads in its working directory. Config reads and writes pass
        None, meaning "any live session will do" — an earlier version demanded a
        match and so tore the session down and rebuilt it (~3s) on every settings
        read, which quietly undid the whole point."""
        if not self._shell_usable():
            return None
        if self._shell is not None and self._shell.alive() \
                and (cwd is None or self._shell.cwd == cwd):
            return self._shell
        cwd = cwd or self._last_shell_cwd or os.getcwd()
        self._last_shell_cwd = cwd
        self._close_shell()
        sh = ShellSession(cwd, SHELL_CAPTURE_STEM + ".%C",
                          log=self._say)
        try:
            sh.start()
        except CameraError as e:
            self._note_shell_failure(f"could not open session: {e}")
            return None
        self._shell = sh
        self._shell_fails = 0
        return sh

    def _close_shell(self) -> None:
        if self._shell is not None:
            self._shell.close()
            self._shell = None

    def _note_shell_failure(self, why: str) -> None:
        self._close_shell()
        self._shell_fails += 1
        self.shell_stats["fallbacks"] += 1
        self._say(f"[camera] persistent session failed ({why}) — using a fresh "
                  f"process for this one [{self._shell_fails}/{_SHELL_MAX_FAILS}]")
        if self._shell_fails >= _SHELL_MAX_FAILS:
            self._shell_off = True
            self._say("[camera] giving up on the persistent gphoto2 session for "
                      "this run; every capture will spawn its own process "
                      "(slower, but reliable). Restart the service to retry.")

    def warmup(self) -> bool:
        """Prove the persistent session works BEFORE any real frame depends on
        it, and switch it off for good if it doesn't.

        This is the guard v0.1.35 lacked. There, the first proof that a session
        was impossible arrived during a capture, and paying for that discovery
        per frame is what stopped the camera taking pictures. Call once at
        startup, before the sensor trigger is armed."""
        if not self._shell_usable():
            return False
        if self._ensure_shell(self._last_shell_cwd) is not None:
            return True
        self._shell_off = True
        self._say("[camera] persistent session unavailable — every operation "
                  "will spawn its own gphoto2 (the proven path). No frames are "
                  "at risk; captures are just slower to the shutter.")
        return False

    def set_capture_dir(self, folder) -> None:
        """Tell the camera where captures will land, before anything opens a
        session. Without this the first session gets rooted at the process's cwd
        (whatever a startup status read happened to use) and the FIRST CAPTURE
        pays a ~3s restart to re-root it — and on this rig the first capture is a
        real slide, not a warm-up."""
        self._last_shell_cwd = str(folder)

    def shutdown(self) -> None:
        """Release the camera. Safe to call more than once, and from a signal
        or restart path — a session left running would keep the USB device
        claimed against whatever starts next."""
        self._close_shell()

    def _rewarm_shell(self) -> None:
        """Reopen the session after an operation that had to close it.

        Done here, on an op nobody is racing, rather than lazily on the next
        capture: restarting costs ~3s, and paid at capture time on this rig that
        is a spoiled frame."""
        if not self._shell_usable() or self._shell is not None:
            return
        if self._last_shell_cwd:
            self._ensure_shell(self._last_shell_cwd)

    def _say(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    @staticmethod
    def _shell_cmds(args: list[str]) -> list[str] | None:
        """Translate a gphoto2 CLI arg list into shell commands, or None if it
        contains anything the shell can't do (which forces the legacy path)."""
        cmds: list[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--get-config":
                cmds.append(f"get-config {args[i + 1]}")
                i += 2
            elif a in ("--set-config", "--set-config-value",
                       "--set-config-index"):
                cmds.append(f"{a[2:]} {args[i + 1]}")
                i += 2
            elif a == "--capture-image-and-download":
                cmds.append("capture-image-and-download")
                i += 1
            elif a == "--filename":       # fixed when the session was launched
                i += 2
            elif a == "--force-overwrite":
                i += 1
            else:
                return None               # e.g. --auto-detect: needs its own run
        return cmds

    def _run(self, args: list[str], timeout: float = 25.0,
             cwd: str | None = None, on_event=None) -> str:
        """Run one gphoto2 command with retry-on-IO-error + backoff. `cwd` sets
        the working directory — gphoto2 needs a WRITABLE cwd to stage a download
        even when --filename is absolute, so captures must run from the output
        dir, not the (root-owned) service WorkingDirectory.

        `on_event` switches to a line-streaming run so the caller learns the
        shutter has fired without waiting for the download (see _run_streaming).

        Prefers the persistent session when one can serve this command; anything
        it can't express closes the session first, because gphoto2 can only have
        one process claiming the camera at a time."""
        cmds = self._shell_cmds(args) if self._shell_usable() else None
        if cmds is not None:
            out = self._try_shell(cmds, timeout, cwd, on_event)
            if out is not None:
                self.shell_stats["shell"] += 1
                self.last_stdout = out
                return out
        else:
            # The legacy path needs exclusive access to the camera.
            self._close_shell()

        last = ""
        for attempt in range(1, self.retries + 1):
            try:
                if on_event is not None:
                    out = self._run_streaming(args, timeout, cwd, on_event)
                    self.last_stdout = out
                    return out
                proc = subprocess.run(
                    ["gphoto2", *args],
                    capture_output=True, text=True, timeout=timeout, cwd=cwd,
                )
            except FileNotFoundError as e:
                raise CameraError("gphoto2 not installed on this host") from e
            except subprocess.TimeoutExpired:
                last = f"timeout after {timeout}s"
            except CameraError as e:               # streaming run, non-zero exit
                last = str(e)
            else:
                if proc.returncode == 0:
                    self.last_stdout = proc.stdout
                    self.shell_stats["legacy"] += 1
                    self._rewarm_shell()
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

    def _try_shell(self, cmds: list[str], timeout: float, cwd: str | None,
                   on_event) -> str | None:
        """Run `cmds` on the persistent session. Returns its output, or None to
        mean "fall back to a fresh process" — never raises, because a session
        problem must not lose the frame."""
        sh = self._ensure_shell(cwd)
        if sh is None:
            return None
        try:
            out = sh.run(cmds, timeout=timeout, on_event=on_event,
                         markers=self._SHUTTER_MARKERS)
        except CameraError as e:
            self._note_shell_failure(str(e))
            return None
        if "*** Error" in out:
            first = next((l for l in out.splitlines() if "*** Error" in l), "?")
            self._note_shell_failure(first.strip())
            return None
        self._shell_fails = 0
        return out

    # -- queries -----------------------------------------------------------

    def detect(self) -> str:
        out = self._run(["--auto-detect"])
        if "usb:" not in out:
            raise CameraError("no camera detected on USB")
        return out.strip()

    def model(self) -> str:
        # Cached: --auto-detect can't run through the persistent session, so an
        # uncached call would tear the session down (and cost the next capture a
        # ~3s restart) purely to re-read a string that never changes.
        if self._model_cache:
            return self._model_cache
        for line in self._run(["--auto-detect"]).splitlines():
            if "usb:" in line:
                self._model_cache = line.rsplit("usb:", 1)[0].strip()
                return self._model_cache
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

    def ready(self) -> bool:
        """One quick probe: True if the camera answers a config read right now.
        Single attempt, no retry/backoff (the caller polls) — a cheap read that
        needs no writable cwd and doesn't touch the shutter.

        Goes through the persistent session when there is one: a separate
        process could not claim the camera anyway while the session holds it.
        A failure here is NOT counted against the session — the camera being
        briefly absent is exactly what this is asking about."""
        sh = self._shell
        if sh is not None and sh.alive():
            try:
                out = sh.run(["get-config availableshots"], timeout=15)
            except CameraError:
                return False
            return "*** Error" not in out
        try:
            proc = subprocess.run(
                ["gphoto2", "--get-config", "availableshots"],
                capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0

    def wait_ready(self, timeout: float = 8.0, poll: float = 0.4,
                   settle: float = 0.5) -> bool:
        """Wait until the camera is ready for the next command, or `timeout`.

        The 400D drops off the USB bus for ~1-2s after each SDRAM capture (it
        re-enumerates), during which PTP ops fail. Polling `ready()` waits
        exactly as long as needed — better than a blind sleep — and returns True
        once it answers again (False on timeout). `settle` is a short initial
        pause so we don't catch the brief still-alive window *before* the drop."""
        if settle:
            time.sleep(settle)
        end = time.monotonic() + timeout
        while True:
            if self.ready():
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(poll)

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

    # gphoto2 prints one of these the moment the camera has the frame — i.e. the
    # exposure is OVER and only the download remains. On a rig where a moving
    # pusher must not disturb the slide before the shutter fires, that instant is
    # the number that matters, so it is reported separately from the ~5s download
    # that follows it.
    _SHUTTER_MARKERS = ("new file is in location", "saving file as")

    def _run_streaming(self, args: list[str], timeout: float, cwd: str | None,
                       on_event) -> str:
        """Like _run's single attempt, but reads stdout line by line so a caller
        can be told the instant the shutter marker appears instead of finding out
        when the whole download finishes. stderr is merged into stdout: we only
        ever use it as error text, and merging avoids a second pipe that could
        fill and deadlock while we are blocked reading the first."""
        proc = subprocess.Popen(["gphoto2", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1,
                                cwd=cwd)
        # The deadline needs its own timer, NOT a check inside the read loop: a
        # wedged gphoto2 prints nothing, so the loop would block in readline()
        # forever and never reach the check. subprocess.run's timeout used to
        # cover this, and losing it would let a sleeping camera pin the camera
        # lock indefinitely.
        out, fired, timed_out = [], False, threading.Event()

        def _kill():
            timed_out.set()
            if proc.poll() is None:
                proc.kill()

        watchdog = threading.Timer(timeout, _kill)
        watchdog.start()
        try:
            for line in proc.stdout:
                out.append(line)
                if not fired and any(m in line.lower()
                                     for m in self._SHUTTER_MARKERS):
                    fired = True
                    if on_event:
                        try:
                            on_event()
                        except Exception:      # never let a callback break a capture
                            pass
            proc.wait()
        finally:
            watchdog.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        if timed_out.is_set():
            # Same wording (and same non-retryable outcome) as the plain path.
            raise CameraError(f"timeout after {timeout}s")
        text = "".join(out)
        if proc.returncode != 0:
            raise CameraError(text.strip() or f"gphoto2 exited {proc.returncode}")
        return text

    def capture(self, dest: Path, capturetarget: str = "Internal RAM",
                on_shutter=None) -> str:
        """Trigger, download to `dest` (may create sibling files for RAW+JPEG).
        `dest` may use gphoto2's %C extension token. Returns the gphoto2 stdout
        (useful for diagnosing an empty download).

        capturetarget defaults to "Internal RAM" (sdram): the frame is buffered
        in the camera and downloaded straight to the host — the reliable, faster
        path on the 400D. With "Memory card", capture-and-download on this body
        writes the frame to the CF card's DCIM folder and does NOT auto-download
        it (gphoto2 only prints "New file is in location … on the camera"), so
        nothing lands locally. Set in the SAME gphoto2 invocation as the capture
        (on a retry the set is a no-op). Pass capturetarget="" to leave the
        camera's current setting untouched.

        `on_shutter` is called the moment the camera reports it has the frame —
        exposure over, download still to come.

        The capturetarget write is sent only ONCE per session (and again after
        any failure), not before every shutter: it is a PTP round-trip sitting
        directly in front of the exposure, and on this rig the sensor-to-shutter
        delay is the throughput limit. If the camera ever loses the setting, the
        capture returns no file and _grab's retry re-sends it."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        args: list[str] = []
        if capturetarget and capturetarget != self._target_set:
            args += ["--set-config-value", f"capturetarget={capturetarget}"]
        # ORDER MATTERS: --filename / --force-overwrite MUST precede the capture
        # action. gphoto2 2.5.28 (on the Pi) ignores a --filename that follows
        # --capture-image-and-download, so the frame is captured but never saved
        # locally (gphoto2 prints only "New file is in location … on the camera"
        # and ~/captures stays empty). Newer gphoto2 tolerated the wrong order.
        args += ["--filename", str(dest), "--force-overwrite",
                 "--capture-image-and-download"]
        # Run from the (writable) output dir — gphoto2 stages the download in
        # cwd, so a non-writable cwd silently drops the file. This, not the
        # arg order or capturetarget, was the real "no downloadable file" cause.
        # 30s is generous (a full RAW cycle is ~5.6s) but bounded, so a wedged
        # camera (asleep / powered off) releases the caller's lock promptly
        # instead of pinning it for the old 90s.
        # A persistent session downloads to the stem fixed at its launch, so
        # clear any leftover from a previous frame before firing.
        self._wipe_shell_stem(dest.parent)
        try:
            out = self._run(args, timeout=30.0, cwd=str(dest.parent),
                            on_event=on_shutter)
        except CameraError:
            self._target_set = None                # re-send it on the next try
            self._wipe_shell_stem(dest.parent)
            raise
        if capturetarget:
            self._target_set = capturetarget
        self._promote_shell_files(dest)
        return out

    @staticmethod
    def _wipe_shell_stem(folder: Path) -> None:
        for f in folder.glob(SHELL_CAPTURE_STEM + ".*"):
            try:
                f.unlink()
            except OSError:
                pass

    @staticmethod
    def _promote_shell_files(dest: Path) -> None:
        """Move a persistent-session capture onto the caller's filename.

        No-op after a legacy run, which writes straight to `dest` — so callers
        get identical results either way and never need to know which path ran.
        `dest` may contain gphoto2's %C extension token."""
        for f in sorted(dest.parent.glob(SHELL_CAPTURE_STEM + ".*")):
            ext = f.suffix.lstrip(".")
            target = Path(str(dest).replace("%C", ext))
            try:
                f.replace(target)
            except OSError:
                pass

    def forget_capturetarget(self) -> None:
        """Drop the cached capturetarget so the next capture re-sends it. Call
        after anything that could have reset the camera's config underneath us
        (a settings change, a reconnect, a capture that downloaded nothing)."""
        self._target_set = None
