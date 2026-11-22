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
NOTIFY = os.environ.get("NOTIFY_SERVICE", "")               # optional, e.g. notify.mobile_app_my_phone
DRY_RUN = os.environ.get("DRY_RUN", "1") != "0"
DATA = pathlib.Path(os.environ.get("DATA_DIR", "/data"))
STATE = DATA / "state.json"
REQUIRED = {"HA_TOKEN": TOKEN, "CAMERA": CAMERA, "HUCKLEBERRY_DEVICE_ID": DEVICE_ID, "SLEEP_SENSOR": SLEEP_SENSOR}

# Tuning knobs (minutes unless noted). Real cribs and real babies need tuning.
INTERVAL = int(os.environ.get("INTERVAL_S", "120"))            # seconds between checks
ASLEEP_CHECKS = int(os.environ.get("ASLEEP_CHECKS", "3"))      # asleep frames in a row to start
WAKE_MIN = float(os.environ.get("WAKE_MIN", "10"))             # awake/empty this long ends the sleep
MIN_SLEEP_MIN = float(os.environ.get("MIN_SLEEP_MIN", "10"))   # shorter than this is discarded
OFFLINE_END_MIN = float(os.environ.get("OFFLINE_END_MIN", "60"))  # camera blind this long ends the sleep
BLIND_ALERT_MIN = float(os.environ.get("BLIND_ALERT_MIN", "15"))  # camera/model failing this long sends a push
RETRY_ALERT_TRIES = int(os.environ.get("RETRY_ALERT_TRIES", "15"))  # failed Huckleberry calls in a row before a push
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
    FIELDS = ("run", "run_start", "start", "last_asleep", "wake_since", "owned", "pending", "tries")

    def __init__(self, saved=None):
        self.run, self.run_start = 0, None      # consecutive asleep frames while idle
        self.start = None                       # sleep start ts while sleeping
        self.last_asleep = self.wake_since = None
        self.owned = False                      # did we start the Huckleberry timer?
        self.pending, self.tries = [], 0        # events not yet delivered to Huckleberry
        for k, v in (saved or {}).items():
            if k in self.FIELDS:
                setattr(self, k, v)

    def dump(self):
        return {k: getattr(self, k) for k in self.FIELDS}

    def step(self, label, t):
        if self.start is None:
            if label == "asleep":
                # asleep frames separated by a long gap are not one streak
                if self.run and (self.last_asleep is None or t - self.last_asleep > WAKE_MIN * 60):
                    self.run = 0
                if self.run == 0:
                    self.run_start = t
                self.run += 1
                self.last_asleep = t
                if self.run >= ASLEEP_CHECKS:
                    self.start, self.last_asleep, self.wake_since, self.run = self.run_start, t, None, 0
                    return [("start", self.start)]
            elif label is not None:
                self.run = 0
            return []
        end = None
        if t - self.last_asleep >= OFFLINE_END_MIN * 60:
            end = self.last_asleep          # checked first: a frame after a long gap belongs to a new sleep
        elif label == "asleep":
            self.last_asleep, self.wake_since = t, None
        elif label is not None:
            if self.wake_since is None:
                self.wake_since = t
            # only a frame that still shows awake/empty confirms the wake-up; blind time does not
            if t - self.wake_since >= WAKE_MIN * 60:
                end = self.wake_since
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
    if raw.get("baby_in_crib") is False:
        return "empty", raw
    ok = raw.get("baby_in_crib") is True and raw.get("state") in ("asleep", "awake")
    return (raw["state"] if ok else None), raw      # a malformed reply counts as unsure


def iso(ts):
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def log(**kw):
    print(json.dumps({"at": iso(time.time()), **kw}), flush=True)


def record(event):
    log(**event)
    try:
        with open(DATA / "events.jsonl", "a") as f:
            f.write(json.dumps(event) + "\n")
    except OSError as e:    # the Huckleberry call already went through; never retry it over a log line
        log(error=f"could not write events.jsonl: {e}"[:200])


def save(tr):
    try:
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(tr.dump()))
        os.replace(tmp, STATE)      # atomic: a kill mid-write cannot leave a half-written file
    except OSError as e:
        log(error=f"could not save state: {e}"[:200])


def timer():
    """Huckleberry timer state and, if one is running, when it started."""
    s = json.loads(ha("/api/states/" + SLEEP_SENSOR))
    if s["state"] not in ("none", "active", "paused"):
        raise RuntimeError("sleep sensor is " + s["state"])     # unavailable: try again later
    cur = (s.get("attributes") or {}).get("current_start")
    return s["state"], (datetime.fromisoformat(cur).timestamp() if cur else None)


def act(tr, ev):
    kind, start = ev[0], ev[1]
    event = {"event": kind, "start": iso(start), "dry_run": DRY_RUN}
    if kind != "start":
        event.update(end=iso(ev[2]), minutes=round((ev[2] - start) / 60))
    if not DRY_RUN:
        state, cur = timer()
        dev = {"device_id": DEVICE_ID}
        if kind == "start":
            if state == "none":
                ha("/api/services/huckleberry/start_sleep", {**dev, "start_time": iso(start)})
                tr.owned = True
            else:
                # A timer is already running. It is ours only if it has our start time, which
                # means an earlier try got through. Anyone else's timer is left alone.
                tr.owned = cur is not None and abs(cur - start) < 90
            event["owned"] = tr.owned
        else:
            # End only the timer we started: one that began after our end time is someone's new one.
            # (The start time itself may have been edited in the app, so it is not compared.)
            event["owned"] = tr.owned
            if tr.owned and state != "none" and (cur is None or cur < ev[2]):
                if kind == "complete":
                    ha("/api/services/huckleberry/complete_sleep", {**dev, "end_time": iso(ev[2])})
                else:
                    ha("/api/services/huckleberry/cancel_sleep", dev)
            else:
                event["skipped"] = "timer not ours or already ended by hand"
            tr.owned = False
    record(event)


def notify(message):
    """Returns False only if a push was wanted and could not be sent, so the caller can try again."""
    log(notify=message, sent=bool(NOTIFY))
    if not NOTIFY:
        return True
    try:
        ha("/api/services/" + NOTIFY.replace(".", "/"), {"title": "Crib watch", "message": message})
        return True
    except Exception as e:  # the push goes through Home Assistant, so it cannot report HA itself being down
        log(error=f"notify failed: {type(e).__name__}: {e}"[:300])
        return False


def flush(tr):
    """Deliver queued events in order. A failed call stays queued and is retried on every
    check until it goes through; nothing is dropped, so a sleep is never left half-logged."""
    while tr.pending:
        ev = tr.pending[0]
        try:
            act(tr, ev)
        except Exception as e:
            tr.tries += 1
            log(error=f"huckleberry call failed: {type(e).__name__}: {e}"[:300], event=ev[0], tries=tr.tries)
            if tr.tries == RETRY_ALERT_TRIES:
                notify(f"Can't send a sleep {ev[0]} to Huckleberry ({tr.tries} tries so far). Still retrying.")
            return
        tr.pending.pop(0)
        tr.tries = 0
        save(tr)    # checkpoint each delivered event, so a restart does not send it twice


def prune_frames():
    cutoff = time.time() - FRAME_KEEP_DAYS * 86400
    for p in (DATA / "frames").glob("*.jpg"):
        if p.stat().st_mtime < cutoff:
            p.unlink()


motion_was_on = None    # unknown until the first valid reading, so a restart is not mistaken for new motion


def wait(tr, t0):
    """Sleep until the next check. During a sleep, new motion cuts the wait short so the
    wake time is caught to within seconds. Motion already under way does not re-trigger."""
    global motion_was_on
    end = max(time.time() + 5, t0 + INTERVAL)
    while time.time() < end:
        time.sleep(max(0, min(10, end - time.time())))
        if not MOTION or tr.start is None:
            continue
        try:
            st = json.loads(ha("/api/states/" + MOTION))["state"]
        except Exception:
            continue
        if st not in ("on", "off"):     # unavailable / unknown is not a reading
            continue
        on = st == "on"
        fresh, motion_was_on = on and motion_was_on is False, on
        if fresh:
            log(msg="motion, looking now")
            return


def main():
    missing = [k for k, v in REQUIRED.items() if not v]
    if missing:
        sys.exit("missing settings: " + ", ".join(missing) + " (see .env.example)")
    (DATA / "frames").mkdir(parents=True, exist_ok=True)
    try:
        saved = json.loads(STATE.read_text()) if STATE.exists() else None
    except ValueError:
        saved = None
        log(error="state.json unreadable, starting fresh")
    tr = Tracker(saved)
    log(msg="sleep logger up", dry_run=DRY_RUN, camera=CAMERA, model=MODEL, state=tr.dump())
    prev, blind_since, blind_told = "?", None, False
    while True:
        t0 = time.time()
        label, raw, jpeg, failed = None, None, None, False
        try:
            jpeg = ha("/api/camera_proxy/" + CAMERA)
            label, raw = classify(jpeg)
        except Exception as e:  # camera offline, tunnel down, model busy: counts as "can't see"
            raw, failed = {"error": f"{type(e).__name__}: {e}"[:200]}, True
        blind_since = (blind_since or t0) if failed else None
        if failed and not blind_told and t0 - blind_since >= BLIND_ALERT_MIN * 60:
            blind_told = notify(f"Can't see the crib for {round((t0 - blind_since) / 60)} minutes: {raw['error']}")
        elif not failed and blind_told:
            blind_told = not notify("Can see the crib again.")
        log(label=label, raw=raw, secs=round(time.time() - t0, 1))
        if jpeg and label != prev:  # keep the frame at every change, to review mistakes later
            try:
                prune_frames()
                (DATA / "frames" / f"{time.strftime('%Y%m%d-%H%M%S')}_{label}.jpg").write_bytes(jpeg)
            except OSError as e:    # a full disk must not stop the sleep tracking
                log(error=f"could not save frame: {e}"[:200])
        prev = label
        tr.pending += [list(ev) for ev in tr.step(label, t0)]
        save(tr)    # queued before the call, so a crash cannot lose it
        flush(tr)
        save(tr)
        wait(tr, t0)


def selftest():
    global ha, MOTION, DRY_RUN, record, save
    globals().update(INTERVAL=120, ASLEEP_CHECKS=3, WAKE_MIN=10, MIN_SLEEP_MIN=10, OFFLINE_END_MIN=60)
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
    # asleep frames either side of a long blind gap are not one streak
    assert run([A, A] + [N] * 10 + [A]) == []
    # one awake frame followed by blind time does not end the sleep
    assert run([A] * 10 + [W] + [N] * 8 + [A, A]) == [("start", 0)]
    # an asleep frame after a 2-hour gap closes the old sleep at its last asleep frame
    tr = Tracker(); [tr.step(A, i * 2 * m) for i in range(10)]
    assert tr.step(A, 18 * m + 7200) == [("complete", 0, 18 * m)]
    # a malformed model reply is "unsure", never "empty"
    for reply, want in (({}, None), ({"baby_in_crib": False}, "empty"), ({"baby_in_crib": True, "state": "awake"}, "awake")):
        _real, globals()["_req"] = _req, lambda *a, **k: json.dumps({"message": {"content": json.dumps(reply)}})
        assert classify(b"x")[0] == want, reply
        globals()["_req"] = _real

    # delivery, against a fake Home Assistant
    fake = {"state": "none", "cur": None, "calls": [], "boom": 0}
    def ha(path, body=None):
        if path.startswith("/api/states/"):
            return json.dumps({"state": fake["state"], "attributes": {"current_start": fake["cur"]}})
        svc = path.rsplit("/", 1)[1]
        fake["calls"].append(svc)
        if svc == "start_sleep":
            fake["state"], fake["cur"] = "active", body["start_time"]
        else:
            fake["state"], fake["cur"] = "none", None
        if fake["boom"]:
            fake["boom"] -= 1
            raise TimeoutError("applied, but the reply was lost")
    DRY_RUN, record, save = False, (lambda e: None), (lambda tr: None)
    # the start goes through but its reply is lost: the retry adopts that timer and does not start another
    tr = Tracker(); tr.pending = [["start", 1000.0], ["complete", 1000.0, 4000.0]]; fake["boom"] = 1
    flush(tr); assert tr.tries == 1 and len(tr.pending) == 2
    flush(tr); assert fake["calls"] == ["start_sleep", "complete_sleep"] and tr.pending == [] and not tr.owned
    # a queued end never touches a timer someone started after that sleep ended
    fake.update(state="active", cur=iso(4100.0), calls=[])
    tr = Tracker(); tr.owned = True; tr.pending = [["complete", 1000.0, 4000.0]]
    flush(tr); assert fake["calls"] == [] and fake["state"] == "active" and tr.pending == []
    # a start never takes over a timer that is already running with a different start time
    tr = Tracker(); tr.pending = [["start", 9000.0]]
    flush(tr); assert fake["calls"] == [] and not tr.owned
    # the sensor being unavailable is retried, not treated as "someone else's timer"
    fake["state"] = "unavailable"; tr.pending = [["start", 9000.0]]
    flush(tr); assert tr.tries == 1 and len(tr.pending) == 1

    # new motion during a sleep cuts the wait short; motion that is still going does not
    clock, feed = [0.0], ["off", "on"]
    time.time, time.sleep = (lambda: clock[0]), (lambda s: clock.__setitem__(0, clock[0] + s))
    feed += []; globals()["motion_was_on"] = None
    MOTION, ha = "x", lambda path, body=None: json.dumps({"state": feed.pop(0) if feed else "on"})
    tr = Tracker(); tr.start = 0
    wait(tr, 0); assert clock[0] == 20, clock
    wait(tr, 20); assert clock[0] == 20 + INTERVAL, clock
    tr.start = None; feed[:] = ["off", "on"]          # not sleeping: motion is ignored
    wait(tr, clock[0]); assert clock[0] == 20 + 2 * INTERVAL, clock
    # after a restart, motion that is already on (or unavailable in between) is not a new edge
    globals()["motion_was_on"] = None; tr.start = 0; feed[:] = ["on", "unavailable", "on"]
    t = clock[0]; wait(tr, t); assert clock[0] == t + INTERVAL, clock
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
