"""Drive SensorTrigger's worker directly (no gpiomon) to check that the phantom
window rejects the camera's transient but honours real slides — including one
dropped mid-capture, which the old settle-and-clear discarded.

Runs at real time scale (the phantom window and lead are absolute seconds), with
the capture shortened to 3s from the ~8s the Pi actually takes.
"""
import sys, threading, time
sys.path.insert(0, "/home/robw/projects/pictureSlideCapture")
import trigger

CAPTURE_SECS = 3.0          # stands in for the ~8s real capture
PHANTOM_AFTER = 0.2         # transient lands this long after USB goes quiet
TAIL = 0.3                  # post-processing between USB going quiet and return


class Rig:
    """One isolated trigger + worker. Each run gets its own, fully joined."""

    def __init__(self, **settings):
        self.shots, self.log, self.timers = [], [], []
        self.busy = False
        self.st = trigger.SensorTrigger({"mode": "gpio", **settings},
                                        on_trigger=self._capture,
                                        log=self.log.append)
        self.worker = threading.Thread(target=self.st._serve, daemon=True)
        self.worker.start()

    def _capture(self, edge_ts=None):
        self.busy = True
        try:
            self.shots.append((f"slide{len(self.shots) + 1}", time.monotonic()))
            time.sleep(CAPTURE_SECS)              # camera busy on USB
            usb_done = time.monotonic()
            t = threading.Timer(PHANTOM_AFTER,    # the 400D's re-enumeration edge
                                lambda: self.st._request(time.monotonic()))
            t.start()
            self.timers.append(t)
            time.sleep(TAIL)
            return usb_done
        finally:
            self.busy = False

    def drop(self):                               # a slide breaks the beam
        self.st._request(time.monotonic())

    def finish(self, quiet=2.5):
        """Wait until it has gone quiet — no new frame and no pending request for
        `quiet` seconds — rather than a fixed delay, so we never stop the worker
        while a capture (or a phantom that has yet to be judged) is still in
        flight. That mistake made every case look like a failure."""
        last = len(self.shots)
        calm = time.monotonic()
        while time.monotonic() - calm < quiet:
            time.sleep(0.1)
            if len(self.shots) != last or self.st._req.is_set() or self.busy:
                last, calm = len(self.shots), time.monotonic()
        self.st._stop.set()
        self.st._req.set()
        self.worker.join(timeout=CAPTURE_SECS + 2)
        for t in self.timers:
            t.cancel()
        assert not self.worker.is_alive(), "worker did not stop"
        return self.shots

    def phantoms(self):
        return [l for l in self.log if "phantom" in l]


def feed(delays, quiet=2.5, **settings):
    rig = Rig(**settings)
    t0 = time.monotonic()
    for d in sorted(delays):
        time.sleep(max(0, t0 + d - time.monotonic()))
        rig.drop()
    return rig, rig.finish(quiet)


fails = []

print("=== 1. one slide: the phantom must not produce a second frame ===")
rig, got = feed([0.0])
ok = len(got) == 1 and len(rig.phantoms()) == 1
print(f"    dropped 1 -> captured {len(got)}, phantoms rejected "
      f"{len(rig.phantoms())}   {'PASS' if ok else 'FAIL'}")
fails += [] if ok else ["1"]

print("\n=== 2. slide dropped DURING a capture is still shot ===")
# lands 1.0s into a 3.0s capture — the old code discarded this one
rig, got = feed([0.0, 1.0])
ok = len(got) == 2
print(f"    dropped 2 (one mid-capture) -> captured {len(got)}   "
      f"{'PASS' if ok else 'FAIL'}")
fails += [] if ok else ["2"]

print("\n=== 3. steady feed: none lost ===")
rig, got = feed([0.0, 5.0, 10.0, 15.0], quiet=3.0)
ok = len(got) == 4
print(f"    dropped 4 -> captured {len(got)}   {'PASS' if ok else 'FAIL'}")
fails += [] if ok else ["3"]

print("\n=== 3b. fast feed: next slide 1.5s after the previous shot finished ===")
# the case the old settle-and-clear lost silently; 3.0s capture + 0.3s tail
rig, got = feed([0.0, 4.8, 9.6], quiet=3.0)
ok = len(got) == 3
print(f"    dropped 3 -> captured {len(got)}   {'PASS' if ok else 'FAIL'}")
fails += [] if ok else ["3b"]

print("\n=== 4. no blanket settle: idle between shots ===")
rig, got = feed([0.0, 1.0])
if len(got) >= 2:
    idle = got[1][1] - (got[0][1] + CAPTURE_SECS)
    ok = idle < 0.6                       # only the post-processing tail
    print(f"    idle between capture 1's USB finishing and capture 2 starting: "
          f"{idle:.2f}s   {'PASS' if ok else 'FAIL (dead time)'}")
    fails += [] if ok else ["4"]
else:
    print(f"    FAIL — only {len(got)} captured")
    fails += ["4"]

print("\n" + ("ALL PASS" if not fails else f"FAILED: {', '.join(fails)}"))
sys.exit(1 if fails else 0)
