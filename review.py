#!/usr/bin/env python3
"""Frame review page for the sleep logger.

Shows the frames the logger saved, one at a time, with the model's read. One tap says
what the frame really shows. Reviewed frames move to data/labeled/ with the answer in
the file name (<time>_<model read>_is_<your answer>.jpg), which builds a test set for
tuning the prompt or trying another model. It never touches Huckleberry.

Stdlib only. There is no login: anyone who can reach the port can see the frames.

Self-check: python3 review.py --selftest
"""
import json, os, pathlib, re, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = pathlib.Path(os.environ.get("DATA_DIR", "/data"))
FRAMES, LABELED = DATA / "frames", DATA / "labeled"
PORT = int(os.environ.get("REVIEW_PORT", "8765"))
LABELS = ("asleep", "awake", "empty", "unsure")
NAME = re.compile(r"\d{8}-\d{6}_[A-Za-z]+(_[a-z]+)?\.jpg", re.ASCII)   # the only file names ever served or moved
DONE = re.compile(r"\d{8}-\d{6}_([A-Za-z]+)(_[a-z]+)?_is_([a-z]+)\.jpg", re.ASCII)
undo_stack = []     # (labeled path, original name), newest last; forgotten on restart


def guess(name):
    return name[:-4].split("_")[1]


def frame(name):
    """Path of a reviewable frame, or None. Regular files with an expected name only: no
    links, nothing outside the frames folder."""
    p = FRAMES / name
    return p if NAME.fullmatch(name) and p.is_file() and not p.is_symlink() else None


def queue():
    """Unreviewed frames, newest first, with awake reads and cry-triggered looks ahead of
    the rest: those are the ones most worth a human look."""
    names = sorted((p.name for p in FRAMES.glob("*.jpg") if frame(p.name)), reverse=True)
    return sorted(names, key=lambda n: not (guess(n) == "awake" or n.endswith("_cry.jpg")))


def status():
    q = queue()
    judged = [m.group(1, 3) for m in map(DONE.fullmatch, (p.name for p in LABELED.glob("*_is_*.jpg"))) if m]
    judged = [(g, b) for g, b in judged if b != "unsure"]
    return {"file": q[0] if q else None, "guess": guess(q[0]) if q else None, "left": len(q),
            "right": sum(g == b for g, b in judged), "judged": len(judged), "can_undo": bool(undo_stack)}


def label(name, answer):
    if answer not in LABELS or not frame(name):
        raise ValueError("bad frame or label")      # also what a second tap on the same frame gets
    LABELED.mkdir(exist_ok=True)
    dst = LABELED / f"{name[:-4]}_is_{answer}.jpg"
    if dst.exists() or dst.is_symlink():
        raise ValueError("already labeled")
    (FRAMES / name).rename(dst)
    undo_stack.append((dst, name))
    del undo_stack[:-50]


def undo():
    if undo_stack:
        dst, name = undo_stack.pop()
        if dst.is_file() and not (FRAMES / name).exists():   # never overwrite a frame that is there now
            dst.rename(FRAMES / name)


class Handler(BaseHTTPRequestHandler):
    timeout = 15    # a stalled phone cannot hold a connection open for ever

    def send(self, code, body, ctype="application/json"):
        body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")   # no other site can embed the page
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self.send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/status":
            self.send(200, status())
        elif self.path.startswith("/frame/") and frame(self.path[7:]):
            try:
                self.send(200, frame(self.path[7:]).read_bytes(), "image/jpeg")
            except (OSError, AttributeError):   # labeled or pruned a moment ago
                self.send(404, {"error": "not found"})
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        # JSON only: a form on some other website cannot send this content type without asking first
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send(415, {"error": "json only"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 <= n <= 1024 or self.headers.get("Transfer-Encoding"):
                raise ValueError("bad length")
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("not an object")
            if self.path == "/label":
                label(str(body.get("file")), str(body.get("label")))
            elif self.path == "/undo":
                undo()
            else:
                return self.send(404, {"error": "not found"})
        except (ValueError, OSError, AttributeError) as e:
            return self.send(400, {"error": str(e)[:100]})
        self.send(200, status())

    def log_message(self, *a):
        pass


PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crib watch review</title>
<style>
  :root { color-scheme: dark light; --bg:#101316; --fg:#eceff2; --dim:#9aa4af; --card:#1b2026; --line:#3a444f; --pick:#7cc4ff; }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f5f6f7; --fg:#14181d; --dim:#566170; --card:#ffffff; --line:#c3cad2; --pick:#0b5cab; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.4 system-ui, -apple-system, sans-serif; }
  main { max-width: 720px; margin: 0 auto; padding: 12px 16px 20px; display: flex; flex-direction: column; gap: 12px; min-height: 100dvh; }
  h1 { font-size: 15px; font-weight: 600; margin: 0; color: var(--dim); }
  #count { font-size: 15px; color: var(--dim); margin: 0; display: flex; justify-content: space-between; gap: 12px; }
  img { width: 100%; aspect-ratio: 16 / 9; object-fit: contain; background: #000; border-radius: 10px; display: block; }
  #said { margin: 0; font-size: 18px; }
  #said b { text-transform: capitalize; }
  .answers { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-top: auto; }
  .more { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  button { font: inherit; color: var(--fg); background: var(--card); border: 2px solid var(--line); border-radius: 12px; min-height: 48px; padding: 6px; cursor: pointer; }
  .answers button { min-height: 76px; font-size: 19px; font-weight: 600; }
  .answers button small { display: block; font-size: 12px; font-weight: 400; color: var(--dim); min-height: 17px; }
  button.guess { border-color: var(--pick); border-width: 3px; }
  button.guess small { color: var(--pick); }
  button:focus-visible { outline: 3px solid var(--pick); outline-offset: 2px; }
  button:disabled { opacity: .45; cursor: default; }
  #done { text-align: center; padding: 48px 0; font-size: 20px; }
  [hidden] { display: none !important; }
</style>
<main>
  <p id="count" aria-live="polite"><span id="left"></span><span id="score"></span></p>
  <h1>What does this frame show?</h1>
  <div id="card">
    <img id="img" alt="Crib camera frame">
    <p id="said"></p>
  </div>
  <p id="done" hidden>All caught up. Nothing to review.</p>
  <p id="msg" role="alert"></p>
  <div class="answers">
    <button data-a="asleep">Asleep<small></small></button>
    <button data-a="awake">Awake<small></small></button>
    <button data-a="empty">Empty<small></small></button>
  </div>
  <div class="more">
    <button data-a="unsure">Can't tell</button>
    <button id="undo">Undo last</button>
  </div>
</main>
<script>
  const $ = id => document.getElementById(id), answers = document.querySelectorAll('[data-a]');
  let cur = null, busy = false;
  async function call(path, body) {
    const r = await fetch(path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {});
    if (!r.ok) throw new Error(r.status);
    return r.json();
  }
  function when(name) {   // 20261122-051245_asleep_cry.jpg
    const m = name.match(/^(\\d{4})(\\d\\d)(\\d\\d)-(\\d\\d)(\\d\\d)(\\d\\d)_[A-Za-z]+_?([a-z]*)/);
    const d = new Date(m[1], m[2] - 1, m[3], m[4], m[5], m[6]);
    return d.toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' }) + (m[7] ? ', after a ' + m[7] + ' trigger' : '');
  }
  function show(s) {
    cur = s.file;
    $('left').textContent = s.left + ' left';
    $('score').textContent = s.judged ? 'Model right ' + s.right + ' of ' + s.judged : '';
    $('card').hidden = !cur; $('done').hidden = !!cur; $('undo').disabled = !s.can_undo; $('msg').textContent = '';
    answers.forEach(b => {
      b.disabled = true;      // until this frame is actually on screen
      const mine = cur && b.dataset.a === s.guess;
      b.classList.toggle('guess', !!mine);
      const small = b.querySelector('small'); if (small) small.textContent = mine ? "model's guess" : '';
    });
    if (!cur) return;
    const want = cur, img = $('img');
    img.onload = () => { if (cur === want) answers.forEach(b => b.disabled = false); };
    img.onerror = () => { if (cur === want) refresh(); };    // labeled on another phone, or cleaned up
    img.src = '/frame/' + cur;
    $('said').innerHTML = 'Model said <b></b>. ' + when(cur);
    $('said').querySelector('b').textContent = s.guess;
  }
  async function send(path, body) {
    if (busy) return; busy = true;
    try { show(await call(path, body)); } catch (e) { await refresh(); $('msg').textContent = 'That did not save. Showing the latest.'; }
    busy = false;
  }
  const refresh = () => call('/status').then(show).catch(() => $('msg').textContent = 'Could not load. Refresh the page.');
  answers.forEach(b => b.onclick = () => cur && send('/label', { file: cur, label: b.dataset.a }));
  $('undo').onclick = () => send('/undo', {});
  document.addEventListener('keydown', e => {
    const a = { 1: 'asleep', 2: 'awake', 3: 'empty', a: 'asleep', w: 'awake', e: 'empty' }[e.key];
    if (a && cur) send('/label', { file: cur, label: a });
    if (e.key === 'u') send('/undo', {});
  });
  refresh();
</script>
</html>
"""


def selftest():
    import tempfile
    global FRAMES, LABELED
    with tempfile.TemporaryDirectory() as d:
        FRAMES, LABELED = pathlib.Path(d) / "frames", pathlib.Path(d) / "labeled"
        FRAMES.mkdir()
        for n in ("20260101-010000_asleep.jpg", "20260101-020000_asleep_cry.jpg", "20260101-030000_awake.jpg",
                  "20260101-040000_empty_motion.jpg", "notes.txt", "..jpg"):
            (FRAMES / n).write_bytes(b"x")
        # awake reads and cry looks come first, then newest first; stray files are ignored
        assert queue() == ["20260101-030000_awake.jpg", "20260101-020000_asleep_cry.jpg",
                           "20260101-040000_empty_motion.jpg", "20260101-010000_asleep.jpg"]
        label("20260101-030000_awake.jpg", "asleep")       # model was wrong
        label("20260101-020000_asleep_cry.jpg", "asleep")  # model was right
        label("20260101-040000_empty_motion.jpg", "unsure")    # not scored
        s = status()
        assert (s["left"], s["right"], s["judged"], s["guess"]) == (1, 1, 2, "asleep"), s
        undo()
        assert status()["left"] == 2 and (FRAMES / "20260101-040000_empty_motion.jpg").is_file()
        # a second tap on a frame that is already labeled is refused, and a stray file does not break the score
        try:
            label("20260101-020000_asleep_cry.jpg", "awake"); raise AssertionError("double label")
        except ValueError:
            pass
        (LABELED / "foo_is_awake.jpg").write_bytes(b"x")
        assert status()["judged"] == 2
        # undo never overwrites a frame that has since appeared under the same name
        label("20260101-010000_asleep.jpg", "asleep"); (FRAMES / "20260101-010000_asleep.jpg").write_bytes(b"new")
        undo(); assert (FRAMES / "20260101-010000_asleep.jpg").read_bytes() == b"new"
        # links are not frames
        (FRAMES / "20260101-050000_asleep.jpg").symlink_to(pathlib.Path(d) / "secret")
        assert frame("20260101-050000_asleep.jpg") is None
        # nothing outside the frames folder, and no made-up labels
        for bad in (("../state.json", "asleep"), ("notes.txt", "asleep"), ("20260101-010000_asleep.jpg", "sleepy"),
                    ("20990101-010000_asleep.jpg", "asleep")):
            try:
                label(*bad); raise AssertionError(bad)
            except ValueError:
                pass
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        FRAMES.mkdir(parents=True, exist_ok=True)
        print(f"review page on port {PORT}", flush=True)
        ThreadingHTTPServer(("", PORT), Handler).serve_forever()
