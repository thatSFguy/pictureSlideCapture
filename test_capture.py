"""Exercise Camera.capture's streaming path against a stub `gphoto2` that mimics
the real one's output timing: it prints the shutter marker, then pauses like a
USB download, then finishes. Checks that on_shutter fires at the marker and NOT
at the end, that capturetarget is sent once and re-sent after a failure, and
that a hung gphoto2 still times out.
"""
import os, stat, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, "/home/robw/projects/pictureSlideCapture")

BIN = Path(tempfile.mkdtemp(prefix="fakebin-"))
OUT = Path(tempfile.mkdtemp(prefix="capout-"))
CALLS = BIN / "calls.log"

STUB = r"""#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
args = sys.argv[1:]
Path(os.environ["CALLS"]).open("a").write(" ".join(args) + "\n")
if "--auto-detect" in args:
    print("Canon EOS 400D (PTP mode)   usb:001,004"); sys.exit(0)
if os.environ.get("FAIL_ONCE") == "1" and "--capture-image-and-download" in args:
    if not Path(os.environ["CALLS"]).with_suffix(".failed").exists():
        Path(os.environ["CALLS"]).with_suffix(".failed").write_text("x")
        print("*** Error: Could not claim the USB device ***"); sys.exit(1)
if "--capture-image-and-download" in args:
    dest = args[args.index("--filename") + 1]
    time.sleep(float(os.environ.get("EXPOSE", "0.4")))     # exposure
    print("New file is in location /capt0000.jpg on the camera", flush=True)
    print("Saving file as " + dest, flush=True)
    time.sleep(float(os.environ.get("DOWNLOAD", "1.2")))   # USB download
    Path(dest.replace("%C", "jpg")).write_bytes(b"\xff\xd8\xff\xd9")
    print("Deleting file /capt0000.jpg on the camera", flush=True)
sys.exit(0)
"""
(BIN / "gphoto2").write_text(STUB)
(BIN / "gphoto2").chmod(0o755)
os.environ["PATH"] = f"{BIN}:{os.environ['PATH']}"
os.environ["CALLS"] = str(CALLS)

import camera

fails = []


def calls():
    return CALLS.read_text().splitlines() if CALLS.exists() else []


def reset():
    CALLS.unlink(missing_ok=True)
    CALLS.with_suffix(".failed").unlink(missing_ok=True)


print("=== 1. on_shutter fires at the marker, not at the end ===")
reset()
cam = camera.Camera(retries=2, backoff=0.1, verbose=False)
fired = []
t0 = time.monotonic()
cam.capture(OUT / "a.%C", on_shutter=lambda: fired.append(time.monotonic() - t0))
total = time.monotonic() - t0
ok = fired and fired[0] < 1.0 and total > 1.4 and (total - fired[0]) > 1.0
print(f"    shutter at +{fired[0]:.2f}s, call returned at +{total:.2f}s "
      f"(download {total - fired[0]:.2f}s hidden behind it)   "
      f"{'PASS' if ok else 'FAIL'}")
fails += [] if ok else ["1"]

print("\n=== 2. capturetarget sent on the first capture, not the second ===")
reset()
cam = camera.Camera(retries=2, backoff=0.1, verbose=False)
cam.capture(OUT / "b.%C")
cam.capture(OUT / "c.%C")
sent = [c for c in calls() if "capturetarget" in c]
ok = len(sent) == 1
print(f"    2 captures -> capturetarget written {len(sent)}x   "
      f"{'PASS' if ok else 'FAIL'}")
fails += [] if ok else ["2"]

print("\n=== 3. after a failure it is re-sent ===")
reset()
os.environ["FAIL_ONCE"] = "1"
cam = camera.Camera(retries=3, backoff=0.1, verbose=False)
cam.capture(OUT / "d.%C")                     # fails once, retries, succeeds
n_after_recovery = len([c for c in calls() if "capturetarget" in c])
cam.capture(OUT / "e.%C")
os.environ["FAIL_ONCE"] = "0"
total_sent = len([c for c in calls() if "capturetarget" in c])
ok = n_after_recovery >= 2 and total_sent == n_after_recovery
print(f"    capturetarget re-sent on the retry ({n_after_recovery} writes), "
      f"then cached again (total {total_sent})   {'PASS' if ok else 'FAIL'}")
fails += [] if ok else ["3"]

print("\n=== 4. the file actually lands, and stdout is returned ===")
reset()
cam = camera.Camera(retries=2, backoff=0.1, verbose=False)
out = cam.capture(OUT / "f.%C")
ok = (OUT / "f.jpg").is_file() and "Saving file as" in out
print(f"    file written: {(OUT / 'f.jpg').is_file()}, stdout captured: "
      f"{'Saving file as' in out}   {'PASS' if ok else 'FAIL'}")
fails += [] if ok else ["4"]

print("\n=== 5. a SILENT hung gphoto2 still times out (no output to react to) ===")
# The killer case: gphoto2 wedged on a sleeping camera prints nothing at all, so
# a deadline checked inside the read loop would never be reached.
reset()
os.environ["EXPOSE"] = "30"          # hangs BEFORE printing anything
cam = camera.Camera(retries=1, backoff=0.1, verbose=False)
t0 = time.monotonic()
try:
    cam._run_streaming(["--filename", str(OUT / "g.%C"), "--force-overwrite",
                        "--capture-image-and-download"],
                       timeout=2.0, cwd=str(OUT), on_event=None)
    ok, why = False, "no error raised"
except camera.CameraError as e:
    el = time.monotonic() - t0
    ok, why = el < 4 and "timeout" in str(e), f"raised after {el:.1f}s: {e}"
os.environ["EXPOSE"] = "0.4"
print(f"    {why}   {'PASS' if ok else 'FAIL'}")
fails += [] if ok else ["5"]

print("\n=== 6. a timeout is NOT retried (same as the non-streaming path) ===")
reset()
os.environ["EXPOSE"] = "30"
cam = camera.Camera(retries=3, backoff=0.1, verbose=False)
try:
    cam._run(["--filename", str(OUT / "h.%C"), "--capture-image-and-download"],
             timeout=1.5, cwd=str(OUT), on_event=lambda: None)
except camera.CameraError:
    pass
os.environ["EXPOSE"] = "0.4"
n = len([c for c in calls() if "--capture-image-and-download" in c])
ok = n == 1
print(f"    gphoto2 invoked {n}x (expected 1, no retry storm)   "
      f"{'PASS' if ok else 'FAIL'}")
fails += [] if ok else ["6"]

print("\n" + ("ALL PASS" if not fails else f"FAILED: {', '.join(fails)}"))
sys.exit(1 if fails else 0)
