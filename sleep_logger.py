#!/usr/bin/env python3
"""Camera sleep logger.

Every INTERVAL seconds: crib camera snapshot (via Home Assistant) -> local vision
model (Ollama) -> asleep / awake / empty -> Huckleberry sleep timer (via Home Assistant).
Stdlib only. DRY_RUN=1 (default) only records what it would have logged.

Self-check: python3 sleep_logger.py --selftest
"""
import base64, json, os, pathlib, sys, time, urllib.request
from datetime import datetime

HA = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
TOKEN = os.environ.get("HA_TOKEN", "")
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("MODEL", "gemma4:12b-it-qat")
CAMERA = os.environ.get("CAMERA", "")                      # e.g. camera.crib
DEVICE_ID = os.environ.get("HUCKLEBERRY_DEVICE_ID", "")    # the child's device in Home Assistant
SLEEP_SENSOR = os.environ.get("SLEEP_SENSOR", "")          # e.g. sensor.<child>_sleep
MOTION = os.environ.get("MOTION_SENSOR", "")                # optional, e.g. binary_sensor.crib_motion
DRY_RUN = os.environ.get("DRY_RUN", "1") != "0"
DATA = pathlib.Path(os.environ.get("DATA_DIR", "/data"))
REQUIRED = {"HA_TOKEN": TOKEN, "CAMERA": CAMERA, "HUCKLEBERRY_DEVICE_ID": DEVICE_ID, "SLEEP_SENSOR": SLEEP_SENSOR}

# Tuning knobs (minutes unless noted). Real cribs and real babies need tuning.
INTERVAL = int(os.environ.get("INTERVAL_S", "120"))            # seconds between checks
ASLEEP_CHECKS = int(os.environ.get("ASLEEP_CHECKS", "3"))      # asleep frames in a row to start
WAKE_MIN = float(os.environ.get("WAKE_MIN", "10"))             # awake/empty this long ends the sleep
MIN_SLEEP_MIN = float(os.environ.get("MIN_SLEEP_MIN", "10"))   # shorter than this is discarded
OFFLINE_END_MIN = float(os.environ.get("OFFLINE_END_MIN", "60"))  # camera blind this long ends the sleep
FRAME_KEEP_DAYS = 3

PROMPT = ("Overhead view of a baby crib (may be night vision). Report whether a baby is in the "
          "crib and whether the baby is asleep (lying still, eyes closed) or awake (eyes open, "
          "sitting, standing, crying).")
SCHEMA = {"type": "object", "required": ["baby_in_crib", "state", "eyes", "adult_present"], "properties": {
    "baby_in_crib": {"type": "boolean"},
    "state": {"type": "string", "enum": ["empty", "asleep", "awake", "unsure"]},
    "eyes": {"type": "string", "enum": ["closed", "open", "not_visible"]},
    "adult_present": {"type": "boolean"}}}


class Tracker:
    """Pure state machine. step(label, t) -> list of events.

    label: "asleep" | "awake" | "empty" | None (unsure / camera or model unavailable).
    events: ("start", start_ts) | ("complete", start_ts, end_ts) | ("cancel", start_ts, end_ts)
    """
    FIELDS = ("run", "run_start", "start", "last_asleep", "wake_since", "owned")

    def __init__(self, saved=None):
        self.run, self.run_start = 0, None      # consecutive asleep frames while idle
        self.start = None                       # sleep start ts while sleeping
        self.last_asleep = self.wake_since = None
        self.owned = False                      # did we start the Huckleberry timer?
        for k, v in (saved or {}).items():
            if k in self.FIELDS:
                setattr(self, k, v)

    def dump(self):
        return {k: getattr(self, k) for k in self.FIELDS}

    def step(self, label, t):
        if self.start is None:
            if label == "asleep":
                if self.run == 0:
                    self.run_start = t
                self.run += 1
                if self.run >= ASLEEP_CHECKS:
                    self.start, self.last_asleep, self.wake_since, self.run = self.run_start, t, None, 0
                    return [("start", self.start)]
            elif label is not None:
                self.run = 0
            return []
        if label == "asleep":
            self.last_asleep, self.wake_since = t, None
        elif label is not None and self.wake_since is None:
            self.wake_since = t
        end = None
        if self.wake_since is not None and t - self.wake_since >= WAKE_MIN * 60:
            end = self.wake_since
        elif t - self.last_asleep >= OFFLINE_END_MIN * 60:
            end = self.last_asleep
        if end is None:
            return []
        start, self.start, self.wake_since = self.start, None, None
        kind = "complete" if end - start >= MIN_SLEEP_MIN * 60 else "cancel"
        return [(kind, start, end)]


def _req(url, body=None, headers=None, timeout=30):
    data = None if body is None else json.dumps(body).encode()
    h = {"Content-Type": "application/json", **(headers or {})}
    with urllib.request.urlopen(urllib.request.Request(url, data, h), timeout=timeout) as r:
        return r.read()


def ha(path, body=None):
    return _req(HA + path, body, {"Authorization": "Bearer " + TOKEN})


def classify(jpeg):
    body = {"model": MODEL, "stream": False, "think": False, "format": SCHEMA, "keep_alive": "10m",
            "options": {"temperature": 0},
            "messages": [{"role": "user", "content": PROMPT, "images": [base64.b64encode(jpeg).decode()]}]}
    raw = json.loads(json.loads(_req(OLLAMA + "/api/chat", body, timeout=300))["message"]["content"])
    if not raw.get("baby_in_crib"):
        return "empty", raw
    return (raw["state"] if raw.get("state") in ("asleep", "awake") else None), raw


def iso(ts):
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def log(**kw):
    print(json.dumps({"at": iso(time.time()), **kw}), flush=True)


def record(event):
    with open(DATA / "events.jsonl", "a") as f:
        f.write(json.dumps(event) + "\n")
    log(**event)


def act(tr, ev):
    kind, start = ev[0], ev[1]
    event = {"event": kind, "start": iso(start), "dry_run": DRY_RUN}
    if kind != "start":
        event.update(end=iso(ev[2]), minutes=round((ev[2] - start) / 60))
    if not DRY_RUN:
        timer = json.loads(ha("/api/states/" + SLEEP_SENSOR))["state"]
        dev = {"device_id": DEVICE_ID}
        if kind == "start":
            # Someone (or something) already has a sleep running: leave it alone.
            tr.owned = timer == "none"
            if tr.owned:
                ha("/api/services/huckleberry/start_sleep", {**dev, "start_time": iso(start)})
        elif tr.owned and timer != "none":
            if kind == "complete":
                ha("/api/services/huckleberry/complete_sleep", {**dev, "end_time": iso(ev[2])})
            else:
                ha("/api/services/huckleberry/cancel_sleep", dev)
        else:
            event["skipped"] = "timer not ours or already ended by hand"
        event["owned"] = tr.owned
    record(event)


def prune_frames():
    cutoff = time.time() - FRAME_KEEP_DAYS * 86400
    for p in (DATA / "frames").glob("*.jpg"):
        if p.stat().st_mtime < cutoff:
            p.unlink()


motion_was_on = False


def wait(tr, t0):
    """Sleep until the next check. During a sleep, new motion cuts the wait short so the
    wake time is caught to within seconds. Motion already under way does not re-trigger."""
    global motion_was_on
    end = max(time.time() + 5, t0 + INTERVAL)
    while time.time() < end:
        time.sleep(min(10, end - time.time()))
        if not MOTION or tr.start is None:
            continue
        try:
            on = json.loads(ha("/api/states/" + MOTION))["state"] == "on"
        except Exception:
            continue
        fresh, motion_was_on = on and not motion_was_on, on
        if fresh:
            log(msg="motion, looking now")
            return


def main():
    missing = [k for k, v in REQUIRED.items() if not v]
    if missing:
        sys.exit("missing settings: " + ", ".join(missing) + " (see .env.example)")
    (DATA / "frames").mkdir(parents=True, exist_ok=True)
    state_file = DATA / "state.json"
    tr = Tracker(json.loads(state_file.read_text()) if state_file.exists() else None)
    log(msg="sleep logger up", dry_run=DRY_RUN, camera=CAMERA, model=MODEL, state=tr.dump())
    prev = "?"
    while True:
        t0 = time.time()
        label, raw, jpeg = None, None, None
        try:
            jpeg = ha("/api/camera_proxy/" + CAMERA)
            label, raw = classify(jpeg)
        except Exception as e:  # camera offline, tunnel down, model busy: counts as "can't see"
            raw = {"error": f"{type(e).__name__}: {e}"[:200]}
        log(label=label, raw=raw, secs=round(time.time() - t0, 1))
        if jpeg and label != prev:  # keep the frame at every change, to review mistakes later
            (DATA / "frames" / f"{time.strftime('%Y%m%d-%H%M%S')}_{label}.jpg").write_bytes(jpeg)
            prune_frames()
        prev = label
        for ev in tr.step(label, t0):
            try:
                act(tr, ev)
            except Exception as e:
                log(error=f"huckleberry call failed: {type(e).__name__}: {e}"[:300], event=ev[0])
        state_file.write_text(json.dumps(tr.dump()))
        wait(tr, t0)


def selftest():
    global ha, MOTION
    m = 60
    def run(seq, step=2 * m):
        tr, out = Tracker(), []
        for i, label in enumerate(seq):
            out += tr.step(label, i * step)
        return out
    A, W, E, N = "asleep", "awake", "empty", None
    # two asleep frames then awake: nothing starts
    assert run([A, A, W, A, A, E]) == []
    # three in a row starts, backdated to the first asleep frame
    assert run([W, A, A, A]) == [("start", 2 * m)]
    # a 6-minute stir does not split the sleep
    assert run([A] * 10 + [W, W, W] + [A] * 3) == [("start", 0)]
    # 10+ minutes awake ends it at the first awake frame
    assert run([A] * 10 + [W] * 6) == [("start", 0), ("complete", 0, 20 * m)]
    # unsure / offline frames neither start nor end anything quickly
    assert run([A, N, A, N, A]) == [("start", 0)]
    assert run([A] * 10 + [N] * 10) == [("start", 0)]
    # camera blind for an hour ends the sleep at the last asleep frame
    assert run([A] * 10 + [N] * 31) == [("start", 0), ("complete", 0, 18 * m)]
    # a "sleep" under 10 minutes is cancelled, not saved
    assert run([A, A, A, E, E, E, E, E, E]) == [("start", 0), ("cancel", 0, 6 * m)]
    # state survives a restart
    tr = Tracker(); [tr.step(A, i * 2 * m) for i in range(5)]
    assert Tracker(json.loads(json.dumps(tr.dump()))).dump() == tr.dump()
    # new motion during a sleep cuts the wait short; motion that is still going does not
    clock, feed = [0.0], ["off", "on"]
    time.time, time.sleep = (lambda: clock[0]), (lambda s: clock.__setitem__(0, clock[0] + s))
    MOTION, ha = "x", lambda path, body=None: json.dumps({"state": feed.pop(0) if feed else "on"})
    tr = Tracker(); tr.start = 0
    wait(tr, 0); assert clock[0] == 20, clock
    wait(tr, 20); assert clock[0] == 20 + INTERVAL, clock
    tr.start = None; feed[:] = ["off", "on"]          # not sleeping: motion is ignored
    wait(tr, clock[0]); assert clock[0] == 20 + 2 * INTERVAL, clock
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
