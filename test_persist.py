"""Persistent gphoto2 session: does it actually remove the per-capture startup
cost, and does it fail safely when the session can't be held?

The stub charges FS_START (3s, the measured Pi cost) for every process launch
and nothing for a command over an open session — so a working persistent session
should show it on capture #1 only.
"""
import os, shutil, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).parent
os.environ["PATH"] = f"{HERE / "tests" / "fakebin"}:{os.environ['PATH']}"
sys.path.insert(0, "/home/robw/projects/pictureSlideCapture")

fails = []


def fresh(**env):
    """A Camera with a clean stub environment and its own output dir."""
    for k in ("FS_DIE_AFTER", "FS_ERR_AFTER", "FS_NO_SHELL", "FS_START",
              "FS_EXPOSE", "FS_DL"):
        os.environ.pop(k, None)
    os.environ["FS_START"] = "3.0"
    os.environ.update({k: str(v) for k, v in env.items()})
    out = Path(tempfile.mkdtemp(prefix="persist-"))
    os.environ["FS_LOG"] = str(out / "calls.log")
    import importlib, camera as camera_mod
    importlib.reload(camera_mod)
    return camera_mod, camera_mod.Camera(retries=2, backoff=0.1, verbose=True), out


def shoot(cam, out, n):
    """n captures; returns the wall time of each."""
    times = []
    for i in range(n):
        t0 = time.monotonic()
        cam.capture(out / f"f{i}.%C")
        times.append(time.monotonic() - t0)
    return times


print("=== 1. startup cost is paid ONCE, not per capture ===")
mod, cam, out = fresh()
t = shoot(cam, out, 4)
ok = t[0] > 2.9 and all(x < 1.5 for x in t[1:])
print(f"    captures: {', '.join(f'{x:.2f}s' for x in t)}")
print(f"    first pays startup, rest are cheap   {'PASS' if ok else 'FAIL'}")
fails += [] if ok else ["1"]
saved = sum(3.0 for _ in t[1:]) - sum(t[1:])
print(f"    -> ~{saved / max(1, len(t) - 1):.2f}s saved per subsequent frame")
cam.shutdown()

print("\n=== 2. the files actually land on the caller's names ===")
mod, cam, out = fresh()
shoot(cam, out, 3)
got = sorted(p.name for p in out.glob("f*.jpg"))
leftover = sorted(p.name for p in out.glob("_gpshell*"))
ok = got == ["f0.jpg", "f1.jpg", "f2.jpg"] and not leftover
print(f"    files: {got}   leftovers: {leftover}   {'PASS' if ok else 'FAIL'}")
fails += [] if ok else ["2"]
cam.shutdown()

print("\n=== 3. config reads/writes go over the session (no new processes) ===")
mod, cam, out = fresh()
cam.capture(out / "a.%C")                    # opens the session
log0 = Path(os.environ["FS_LOG"]).read_text().count("shell START")
t0 = time.monotonic()
cam.get_many(["batterylevel", "iso", "shutterspeed"])
cam.configure({"iso": "200"})
el = time.monotonic() - t0
log1 = Path(os.environ["FS_LOG"]).read_text()
ok = el < 1.5 and log1.count("shell START") == log0 and "cli " not in log1
print(f"    4 config ops in {el:.2f}s, extra processes spawned: "
      f"{log1.count('cli ')}   {'PASS' if ok else 'FAIL'}")
fails += [] if ok else ["3"]
cam.shutdown()

print("\n=== 4. session dies mid-run -> frame is still captured ===")
mod, cam, out = fresh(FS_DIE_AFTER=2)
try:
    t = shoot(cam, out, 4)
    got = sorted(p.name for p in out.glob("f*.jpg"))
    ok = len(got) == 4
except mod.CameraError as e:
    ok, got = False, f"raised {e}"
print(f"    4 requested, {got if isinstance(got, str) else len(got)} captured, "
      f"fallbacks={cam.shell_stats['fallbacks']}   {'PASS' if ok else 'FAIL'}")
fails += [] if ok else ["4"]
cam.shutdown()

print("\n=== 5. a camera that can't hold a session -> gives up, keeps working ===")
mod, cam, out = fresh(FS_NO_SHELL=1)
t = shoot(cam, out, 5)
got = sorted(p.name for p in out.glob("f*.jpg"))
ok = len(got) == 5 and cam._shell_off and cam.shell_stats["legacy"] >= 5
print(f"    {len(got)}/5 captured, gave up on the session: {cam._shell_off}, "
      f"legacy runs: {cam.shell_stats['legacy']}   {'PASS' if ok else 'FAIL'}")
fails += [] if ok else ["5"]

print("\n=== 6. after giving up it stops retrying (no wasted attempts) ===")
attempts = Path(os.environ["FS_LOG"]).read_text().count("shell REFUSED")
ok = attempts <= 3
print(f"    session attempts before giving up: {attempts} (cap 3)   "
      f"{'PASS' if ok else 'FAIL'}")
fails += [] if ok else ["6"]
cam.shutdown()

print("\n=== 7. shutter callback still fires early, over the session ===")
mod, cam, out = fresh(FS_EXPOSE=0.3, FS_DL=1.5)
cam.capture(out / "warm.%C")                 # pay the startup
fired = []
t0 = time.monotonic()
cam.capture(out / "timed.%C", on_shutter=lambda: fired.append(time.monotonic() - t0))
total = time.monotonic() - t0
ok = fired and fired[0] < 0.9 and (total - fired[0]) > 1.2
print(f"    shutter at +{fired[0]:.2f}s, returned at +{total:.2f}s   "
      f"{'PASS' if ok else 'FAIL'}")
fails += [] if ok else ["7"]
cam.shutdown()

print("\n=== 8. an untranslatable op reopens the session for the next capture ===")
mod, cam, out = fresh()
cam.capture(out / "x.%C")
cam._model_cache = ""                        # force a real --auto-detect
cam.model()                                  # must close, then rewarm
t0 = time.monotonic()
cam.capture(out / "y.%C")
el = time.monotonic() - t0
ok = el < 1.5 and cam._shell is not None
print(f"    capture after --auto-detect took {el:.2f}s (session warm again)   "
      f"{'PASS' if ok else 'FAIL'}")
fails += [] if ok else ["8"]
cam.shutdown()

print("\n" + ("ALL PASS" if not fails else f"FAILED: {', '.join(fails)}"))
sys.exit(1 if fails else 0)
