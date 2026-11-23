#!/usr/bin/env python3
"""Listen to the crib on your network: an audio-only stream plus a Bonjour (mDNS) name for it.

It relays the sound that the cry-alert container already receives, and nothing else: no video,
and none of that container's other controls. There is no login. Run `listen.py --selftest`
to check the logic.
"""
import http.client
import os
import socket
import struct
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("LISTEN_PORT", "8766"))
NAME = os.environ.get("LISTEN_NAME", "crib-audio").strip().lower()   # <name>.local; empty turns Bonjour off
SOURCE = os.environ.get("LISTEN_SOURCE", "http://127.0.0.1:1984/api/stream.aac?src=crib")
IP = os.environ.get("LISTEN_IP", "")            # address to advertise; found automatically if empty
MAX_LISTENERS = 8
RETRY = 2                                       # seconds between tries when the camera's sound drops
MDNS = ("224.0.0.251", 5353)
TTL = 120
SERVICE = "_http._tcp.local"

PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Crib audio</title>
<style>:root{color-scheme:dark light}body{font:17px system-ui;margin:0;min-height:100vh;display:grid;
place-content:center;gap:20px;text-align:center;padding:16px}button{font:inherit;font-size:22px;
padding:22px 44px;border-radius:14px;border:0;background:#2d6cdf;color:#fff}p{margin:0;opacity:.7}</style>
<button id="b">Listen</button><p id="s" role="status">Stopped</p><audio id="a" preload="none"></audio>
<script>
const a=document.getElementById("a"),b=document.getElementById("b"),s=document.getElementById("s");
let on=false,timer;
function play(){clearTimeout(timer);a.src="/stream.aac?"+Date.now();a.play().catch(()=>retry())}
function retry(){if(!on)return;s.textContent="Reconnecting";clearTimeout(timer);timer=setTimeout(play,2000)}
b.onclick=()=>{on=!on;b.textContent=on?"Stop":"Listen";
  if(on){s.textContent="Connecting";play()}else{clearTimeout(timer);a.pause();a.removeAttribute("src");a.load();s.textContent="Stopped"}};
a.onplaying=()=>{if(on)s.textContent="Live"};
a.onwaiting=()=>{if(on)s.textContent="Waiting for sound"};
a.onerror=a.onended=retry;
// lock screen and control centre: a title, and play and pause buttons that work without unlocking
if("mediaSession" in navigator){
  navigator.mediaSession.metadata=new MediaMetadata({title:"Crib audio",artist:"Live"});
  navigator.mediaSession.setActionHandler("play",()=>{if(on){s.textContent="Connecting";play()}else b.onclick()});
  navigator.mediaSession.setActionHandler("pause",()=>{if(on)b.onclick()});
}
</script></html>""".encode()

slots = threading.BoundedSemaphore(MAX_LISTENERS)


def keepalive(sock):
    """Notice within about a minute that a phone vanished (left Wi-Fi) while nothing is being sent to it."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for opt, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
        if hasattr(socket, opt):                    # Linux names; elsewhere the system defaults apply
            sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), value)


class Handler(BaseHTTPRequestHandler):
    timeout = 15

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(PAGE)
        elif path == "/stream.aac":
            self.stream()
        else:
            self.send_error(404)

    def stream(self):
        if not slots.acquire(blocking=False):
            return self.send_error(503, "Too many listeners")
        keepalive(self.connection)
        started = False
        try:
            while True:
                try:
                    with urllib.request.urlopen(SOURCE, timeout=10) as up:
                        if not started:
                            started = True                      # from here on, never a second reply
                            self.send_response(200)
                            self.send_header("Content-Type", "audio/aac")
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("Connection", "close")
                            self.end_headers()
                            self.connection.settimeout(15)      # a listener that stops reading is dropped
                        while chunk := up.read1(4096):
                            try:
                                self.wfile.write(chunk)
                            except OSError:
                                return                          # the listener left
                except (OSError, http.client.HTTPException):
                    if not started:
                        return self.send_error(502, "No audio from the camera")
                # The camera's sound dropped. Keep this listener and reconnect, because a locked
                # phone cannot reconnect by itself. A listener that left is noticed on the next write.
                time.sleep(RETRY)
        except OSError:
            pass
        finally:
            self.close_connection = True
            slots.release()


# --- Bonjour: just enough mDNS to answer for one name and one web service ---

def enc(name):
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"


def dec(buf, i):
    """Read a DNS name at offset i. Returns (name, offset after it)."""
    parts, end, hops = [], None, 0
    while True:
        n = buf[i]
        if n & 0xC0 == 0xC0:                        # pointer to an earlier name
            end = end or i + 2
            to = ((n & 0x3F) << 8) | buf[i + 1]
            hops += 1
            if to >= i or hops > 10:
                raise ValueError("bad pointer")
            i = to
        elif n == 0:
            return ".".join(parts).lower(), end or i + 1
        else:
            parts.append(buf[i + 1:i + 1 + n].decode("utf-8", "replace"))
            i += 1 + n


def questions(buf):
    flags, count = struct.unpack("!HH", buf[2:6])
    if flags & 0x8000:                              # a response, not a question
        return []
    out, i = [], 12
    for _ in range(count):
        name, i = dec(buf, i)
        if i + 4 > len(buf):
            raise ValueError("cut short")
        out.append(name)
        i += 4
    return out


def rr(name, rtype, data, flush=True):
    return enc(name) + struct.pack("!HHIH", rtype, 0x8001 if flush else 1, TTL, len(data)) + data


def answer(names, ip, name=None, port=None):
    """The mDNS reply for these question names, or None if none of them is ours."""
    name, port = name or NAME, port or PORT
    host, inst = f"{name}.local", f"{name}.{SERVICE}"
    if not set(names) & {host, inst, SERVICE, "_services._dns-sd._udp.local"}:
        return None
    rrs = [rr(host, 1, socket.inet_aton(ip)),
           # "this name has an IPv4 address and nothing else", or Apple devices wait 5 seconds for IPv6
           rr(host, 47, enc(host) + b"\x00\x01\x40"),
           rr(SERVICE, 12, enc(inst), flush=False),
           rr(inst, 33, struct.pack("!HHH", 0, 0, port) + enc(host)),
           rr(inst, 16, b"\x06path=/"),
           rr("_services._dns-sd._udp.local", 12, enc(SERVICE), flush=False)]
    return struct.pack("!HHHHHH", 0, 0x8400, 0, len(rrs), 0, 0) + b"".join(rrs)


def my_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))                 # sends nothing; only asks which address would be used
        return s.getsockname()[0]
    finally:
        s.close()


def bonjour():
    while True:
        try:
            advertise(IP or my_ip())
        except OSError as e:                        # network not up yet, or it went away: try again
            print(f"bonjour: {e}; retrying in 30 seconds", flush=True)
            time.sleep(30)


def advertise(ip):
    print(f"bonjour: http://{NAME}.local{'' if PORT == 80 else f':{PORT}'} is {ip}", flush=True)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)     # share 5353 with the host's own mDNS
    s.bind(("", 5353))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(MDNS[0]) + socket.inet_aton(ip))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    s.settimeout(60)
    hello = answer([SERVICE], ip)
    s.sendto(hello, MDNS)
    while True:
        try:
            buf, _ = s.recvfrom(9000)
            reply = answer(questions(buf), ip)
            if reply:
                s.sendto(reply, MDNS)
        except socket.timeout:
            # ponytail: re-announce every quiet minute instead of probing and conflict handling;
            # pick another LISTEN_NAME if something else on the network already uses this one
            s.sendto(hello, MDNS)
        except (ValueError, IndexError, struct.error):
            pass                                    # a packet we cannot read is not ours to answer


def selftest():
    q = struct.pack("!HHHHHH", 0, 0, 2, 0, 0, 0) + enc("crib-audio.local") + b"\0\x01\0\x01" + b"\xc0\x0c\0\x1c\0\x01"
    assert questions(q) == ["crib-audio.local", "crib-audio.local"]         # second name is a pointer
    assert questions(struct.pack("!HHHHHH", 0, 0x8400, 1, 0, 0, 0) + enc("crib-audio.local") + b"\0\x01\0\x01") == []
    try:
        dec(b"\xc0\x00", 0)
        raise AssertionError("pointer to itself not caught")
    except ValueError:
        pass
    assert answer(["printer.local"], "192.0.2.7", "crib-audio", 8766) is None
    reply = answer(["crib-audio.local"], "192.0.2.7", "crib-audio", 8766)
    assert enc("crib-audio.local") + b"\0\x2f" in reply                   # the no-IPv6 record
    assert socket.inet_aton("192.0.2.7") in reply and struct.pack("!HHH", 0, 0, 8766) in reply
    assert dec(reply, 12)[0] == "crib-audio.local"
    assert answer([SERVICE], "192.0.2.7", "crib-audio", 8766) == reply

    # the relay passes audio through, serves nothing but the page and the stream, and caps listeners
    class Fake(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(1)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"\xff\xf1audio%d" % len(hits))

    hits = []
    up = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    me = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    for srv in (up, me):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    globals()["SOURCE"] = f"http://127.0.0.1:{up.server_port}/api/stream.aac?src=crib"
    base = f"http://127.0.0.1:{me.server_port}"

    def get(path):
        try:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    globals()["RETRY"] = 0.05
    with urllib.request.urlopen(base + "/stream.aac", timeout=5) as r:      # the fake camera hangs up after
        assert r.read(16) == b"\xff\xf1audio1\xff\xf1audio2"                 # each reply: the relay reconnects
    assert get("/")[1] == PAGE
    assert get("/api/streams")[0] == 404 and get("/stream.aac/../api")[0] == 404
    for _ in range(MAX_LISTENERS):
        assert slots.acquire(timeout=5), "a finished listener did not give its slot back"
    assert get("/stream.aac")[0] == 503
    for _ in range(MAX_LISTENERS):
        slots.release()
    up.shutdown()
    globals()["SOURCE"] = "http://127.0.0.1:1/"
    assert get("/stream.aac")[0] == 502
    print("selftest ok")


if __name__ == "__main__":
    # no argument runs both halves; --web or --bonjour runs one (see compose.yaml for why they are split)
    if "--selftest" in sys.argv:
        selftest()
        sys.exit()
    web, mdns = "--bonjour" not in sys.argv, "--web" not in sys.argv and bool(NAME)
    if web and mdns:
        threading.Thread(target=bonjour, daemon=True).start()
    if web:
        ThreadingHTTPServer(("", PORT), Handler).serve_forever()
    elif mdns:
        bonjour()
    else:
        print("bonjour is off (LISTEN_NAME is empty)", flush=True)
        threading.Event().wait()    # stay up, so Docker does not restart this in a loop
