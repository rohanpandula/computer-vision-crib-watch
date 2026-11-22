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
CRY = os.environ.get("CRY_SENSOR", "")                      # optional, e.g. binary_sensor.crib_cry
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
RECENT = 600    # seconds: how long a cry stays relevant, and the longest gap inside one asleep streak

PROMPT = ("Overhead view of a baby crib (may be night vision). Report whether a baby is in the "
          "crib and whether the baby is asleep (lying still, eyes closed) or awake (eyes open, "
          "sitting, standing, crying). A baby counts as in the crib only if you can see its head or "
          "face. An empty sleep sack, swaddle, blanket, clothing or toy lying in the crib is not a baby.")
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
    FIELDS = ("run", "run_start", "start", "last_asleep", "wake_since", "owned", "pending", "tries", "cry_at")

    def __init__(self, saved=None):
        self.run, self.run_start = 0, None      # consecutive asleep frames while idle
        self.start = None                       # sleep start ts while sleeping
        self.last_asleep = self.wake_since = None
        self.owned = False                      # did we start the Huckleberry timer?
        self.pending, self.tries = [], 0        # events not yet delivered to Huckleberry
        self.cry_at = None                      # first cry of the current bout, while sleeping
        for k, v in (saved or {}).items():
            if k in self.FIELDS:
                setattr(self, k, v)

    def dump(self):
        return {k: getattr(self, k) for k in self.FIELDS}

    def cry(self, t):
        """A cry was heard at t. Kept as the likely wake time if the camera then agrees."""
        if self.start is not None and t > self.start and (self.cry_at is None or t - self.cry_at > RECENT):
            self.cry_at = t

    def step(self, label, t):
        if self.start is None:
            if label == "asleep":
                # asleep frames separated by a long gap are not one streak
                if self.run and (self.last_asleep is None or t - self.last_asleep > RECENT):
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
                # woke crying a few minutes ago: the cry, not this frame, is when the sleep ended
                recent = self.cry_at is not None and 0 <= t - self.cry_at <= RECENT
                self.wake_since = self.cry_at if recent else t
            # only a frame that still shows awake/empty confirms the wake-up; blind time does not
            if t - self.wake_since >= WAKE_MIN * 60:
                end = self.wake_since
        if end is None:
            return []
        start, self.start, self.wake_since, self.cry_at = self.start, None, None, None
        kind = "complete" if end - start >= MIN_SLEEP_MIN * 60 else "cancel"
        return [(kind, start, end)]


def _req(url, body=None, headers=None, timeout=30):
    data = None if body is None else json.dumps(body).encode()
    h = {"Content-Type": "application/json", "User-Agent": "crib-watch/1.0", **(headers or {})}
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


def pushes(title, message):
    """(name, url, body, headers) for every push platform that has its settings filled in.
    Only the Home Assistant one has been used for real; the rest follow each service's
    documented API but are untested."""
    E, both = os.environ.get, f"{title}: {message}"
    if NOTIFY:
        yield ("home assistant", HA + "/api/services/" + NOTIFY.replace(".", "/"),
               {"title": title, "message": message}, {"Authorization": "Bearer " + TOKEN})
    if E("NTFY_TOPIC"):
        auth = {"Authorization": "Bearer " + E("NTFY_TOKEN")} if E("NTFY_TOKEN") else {}
        yield ("ntfy", E("NTFY_URL", "https://ntfy.sh"), {"topic": E("NTFY_TOPIC"), "title": title, "message": message}, auth)
    if E("PUSHOVER_TOKEN") and E("PUSHOVER_USER"):
        yield ("pushover", "https://api.pushover.net/1/messages.json",
               {"token": E("PUSHOVER_TOKEN"), "user": E("PUSHOVER_USER"), "title": title, "message": message}, {})
    if E("PUSHBULLET_TOKEN"):
        yield ("pushbullet", "https://api.pushbullet.com/v2/pushes",
               {"type": "note", "title": title, "body": message}, {"Access-Token": E("PUSHBULLET_TOKEN")})
    if E("TELEGRAM_BOT_TOKEN") and E("TELEGRAM_CHAT_ID"):
        yield ("telegram", f"https://api.telegram.org/bot{E('TELEGRAM_BOT_TOKEN')}/sendMessage",
               {"chat_id": E("TELEGRAM_CHAT_ID"), "text": both}, {})
    if E("GOTIFY_URL") and E("GOTIFY_TOKEN"):
        yield ("gotify", E("GOTIFY_URL").rstrip("/") + "/message",
               {"title": title, "message": message, "priority": 5}, {"X-Gotify-Key": E("GOTIFY_TOKEN")})
    if E("DISCORD_WEBHOOK"):
        yield ("discord", E("DISCORD_WEBHOOK"), {"content": both}, {})
    if E("SLACK_WEBHOOK"):
        yield ("slack", E("SLACK_WEBHOOK"), {"text": both}, {})
    if E("WEBHOOK_URL"):
        yield ("webhook", E("WEBHOOK_URL"), {"title": title, "message": message}, {})


def notify(message):
    """Send to every configured platform. Returns False only if pushes were wanted and none
    got through, so the caller can try again. A push cannot report its own platform being down."""
    sent, wanted = [], 0
    for name, url, body, headers in pushes("Crib watch", message):
        wanted += 1
        try:
            _req(url, body, headers, timeout=10)
            sent.append(name)
        except Exception as e:  # name only: some of these URLs contain the secret
            log(error=f"{name} push failed: {type(e).__name__}"[:300])
    log(notify=message, sent=sent)
    return bool(sent) or not wanted


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
            if tr.tries % RETRY_ALERT_TRIES == 0:
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


seen = {}   # sensor -> (state, last_changed) at the last poll


def fired(sensor):
    """Time of new activity on a binary sensor since the last poll, else None. Fires when a
    sensor that was off has changed: it is now on, or it blipped on and off between polls.
    The first reading after a (re)start is only a baseline, and "unavailable" is not a reading."""
    s = json.loads(ha("/api/states/" + sensor))
    if s["state"] not in ("on", "off"):
        return None
    old, seen[sensor] = seen.get(sensor), (s["state"], s["last_changed"])
    if old is None or old[0] != "off" or old[1] == s["last_changed"]:
        return None
    return datetime.fromisoformat(s["last_changed"]).timestamp()


def wait(tr, t0):
    """Sleep until the next check. During a sleep, new motion or a cry cuts the wait short so
    the wake time is caught to within seconds. Activity already under way does not re-trigger."""
    end = max(time.time() + 5, t0 + INTERVAL)
    while time.time() < end:
        time.sleep(max(0, min(10, end - time.time())))
        if tr.start is None:
            seen.clear()
            continue
        why = []
        for name, sensor in (("cry", CRY), ("motion", MOTION)):
            try:
                at = fired(sensor) if sensor else None
            except Exception:
                continue
            if at is not None:
                why.append(name)
                if name == "cry":
                    tr.cry(at)
        if why:
            log(msg=" and ".join(why) + ", looking now")
            return why
    return []


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
    prev, blind_since, blind_told, why, last_frame = "?", None, False, [], 0
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
        # Keep the frames worth a second look (see review.py): every change, every look a cry
        # asked for, and awake reads and motion looks at most once every 10 minutes.
        keep = label != prev or "cry" in why or ((label == "awake" or "motion" in why) and t0 - last_frame >= 600)
        try:
            prune_frames()
        except OSError:
            pass
        if jpeg and keep:
            try:
                name = f"{time.strftime('%Y%m%d-%H%M%S')}_{label or 'unsure'}{'_' + why[0] if why else ''}.jpg"
                (DATA / "frames" / name).write_bytes(jpeg)
                last_frame = t0
            except OSError as e:    # a full disk must not stop the sleep tracking
                log(error=f"could not save frame: {e}"[:200])
        prev = label
        tr.pending += [list(ev) for ev in tr.step(label, t0)]
        save(tr)    # queued before the call, so a crash cannot lose it
        flush(tr)
        save(tr)
        why = wait(tr, t0)


def selftest():
    global ha, MOTION, CRY, DRY_RUN, record, save
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
    # woke crying: the sleep ends at the cry, even though frames kept reading asleep for a bit
    tr = Tracker(); [tr.step(A, i * 2 * m) for i in range(10)]
    tr.cry(19 * m); tr.cry(19.5 * m)
    assert tr.step(A, 20 * m) == [] and [tr.step(E, k * m) for k in (22, 24, 26, 28)] == [[]] * 4
    assert tr.step(E, 30 * m) == [("complete", 0, 19 * m)] and tr.cry_at is None
    # a cry she settled from is forgotten: a later wake-up ends at the first awake frame
    tr = Tracker(); [tr.step(A, i * 2 * m) for i in range(10)]
    tr.cry(5 * m)
    assert [tr.step(W, k * m) for k in (20, 26)] == [[], []] and tr.step(W, 30 * m) == [("complete", 0, 20 * m)]
    # a short wake wait (WAKE_MIN=2) still lets a sleep start, and ends it on the second empty frame
    globals()["WAKE_MIN"] = 2
    assert run([A] * 6 + [E, E]) == [("start", 0), ("complete", 0, 12 * m)]
    assert run([A] * 6 + [E, A, A]) == [("start", 0)]
    globals()["WAKE_MIN"] = 10
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

    # every push platform builds a request, one failing does not stop the rest, and no secret is logged
    env = dict(NTFY_TOPIC="t", PUSHOVER_TOKEN="SECRET", PUSHOVER_USER="u", PUSHBULLET_TOKEN="SECRET",
               TELEGRAM_BOT_TOKEN="SECRET", TELEGRAM_CHAT_ID="1", GOTIFY_URL="http://g/", GOTIFY_TOKEN="SECRET",
               DISCORD_WEBHOOK="http://d/SECRET", SLACK_WEBHOOK="http://s/SECRET", WEBHOOK_URL="http://w/SECRET")
    os.environ.update(env); globals()["NOTIFY"] = "notify.phone"
    got = list(pushes("T", "M"))
    assert [g[0] for g in got] == ["home assistant", "ntfy", "pushover", "pushbullet", "telegram", "gotify", "discord", "slack", "webhook"]
    assert all(u.startswith("http") and json.dumps(b) for _, u, b, _ in got) and got[5][1] == "http://g/message"
    def fake_req(url, body=None, headers=None, timeout=30):
        if "telegram" in url:
            raise OSError(url)
    out, real_req, real_log = [], _req, log
    globals().update(_req=fake_req, log=lambda **kw: out.append(json.dumps(kw)))
    assert notify("M") is True and "telegram push failed" in "".join(out) and "SECRET" not in "".join(out)
    globals()["_req"] = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
    assert notify("M") is False
    globals().update(_req=real_req, log=real_log, NOTIFY="")
    for k in env:
        del os.environ[k]
    assert notify("nothing configured") is True

    # sensor triggers, on a fake clock
    clock, feeds = [0.0], {}
    time.time, time.sleep = (lambda: clock[0]), (lambda s: clock.__setitem__(0, clock[0] + s))
    def ha(path, body=None):    # each sensor replays its feed of (state, changed-at seconds), then holds the last
        f = feeds[path.rsplit("/", 1)[1]]
        st, at = f.pop(0) if len(f) > 1 else f[0]
        return json.dumps({"state": st, "last_changed": iso(at)})
    MOTION, CRY = "m", ""
    # new motion during a sleep cuts the wait short; motion that is still going does not
    tr = Tracker(); tr.start = 0; seen.clear(); feeds["m"] = [("off", 0), ("on", 15)]
    wait(tr, 0); assert clock[0] == 20, clock
    wait(tr, 20); assert clock[0] == 20 + INTERVAL, clock
    # not sleeping: sensors are ignored
    tr.start = None; feeds["m"] = [("off", 200), ("on", 210)]
    t = clock[0]; wait(tr, t); assert clock[0] == t + INTERVAL, clock
    # after a restart, motion already on (or unavailable in between) is not a new edge
    tr.start = 0; seen.clear(); feeds["m"] = [("on", 300), ("unavailable", 310), ("on", 320)]
    t = clock[0]; wait(tr, t); assert clock[0] == t + INTERVAL, clock
    # a cry that came and went between two polls still counts, and is remembered as the cry time
    MOTION, CRY = "", "c"
    seen.clear(); t = clock[0]; feeds["c"] = [("off", 0), ("off", t + 14)]
    wait(tr, t); assert clock[0] == t + 20 and tr.cry_at == t + 14, (clock, tr.cry_at)
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
