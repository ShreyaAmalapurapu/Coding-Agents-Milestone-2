#!/usr/bin/env python3
"""Checks for component E (message edits, IRC gateway, web UI). Prints PASS/FAIL per rule.

CHECK_STAGE=1..7 runs one stage (one spec file). There is no "all": stage 3 kills an HTTP node and
waits for it, and stages 5 to 7 drive a real browser, so together they would not fit in 30 seconds.
Processes are only ever killed by the PID that owns a port (from `ss -ltnp`), never by name.
"""
import asyncio
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import websockets
except ImportError:  # the WebSocket rules then fail with a clear message
    websockets = None

N0, N1, N2 = "http://127.0.0.1:8000", "http://127.0.0.1:8001", "http://127.0.0.1:8002"
NODES = (N0, N1, N2)
PW = "correct-horse-1"
RUN = "%x%x" % (int(time.time()), random.randrange(4096))
T0 = time.time()
# AIDEV-NOTE: the agent's commands are cut off after 30s. Requests stop at DEADLINE and a watchdog
# prints every unreached rule as FAIL plus the RESULT line at 26s, so the summary is never lost.
DEADLINE = 25

STAGES = {1: range(1, 10), 2: range(10, 18), 3: range(18, 26), 4: range(26, 31), 5: range(31, 38),
          6: range(38, 46), 7: range(46, 54)}
SELECTED = [int(x) for x in os.environ.get("CHECK_STAGE", "1").split(",")]


def _feedback():
    """The testing mode, read from the FEEDBACK= line of lib.sh next to this file."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib.sh")) as f:
            for line in f:
                m = re.match(r"\s*FEEDBACK=(\w+)", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return "verbose"


FEEDBACK = _feedback()


def say(text):
    """Prints a PASS, FAIL or NOTE line, only in verbose mode."""
    if FEEDBACK == "verbose":
        print(text, flush=True)


passed = failed = 0
done = set()
_lock = threading.Lock()


def want(rule):
    return any(rule in STAGES[s] for s in SELECTED)


def _time_is_up():
    with _lock:
        missing = [r for s in SELECTED for r in STAGES[s] if r not in done]
        for r in missing:
            say("FAIL %s not reached: the check's time budget ran out (earlier rules were slow)" % r)
        print("RESULT: %d passed, %d failed" % (passed, failed + len(missing)), flush=True)
        os._exit(1)


def ok(rule, msg):
    global passed
    with _lock:
        if rule in done:
            return
        done.add(rule)
        if want(rule):
            passed += 1
    page_notes()


def bad(rule, msg):
    global failed
    msg += page_notes()
    with _lock:
        if rule in done:
            return
        done.add(rule)
        if want(rule):
            failed += 1
            say("FAIL %s %s" % (rule, msg))


def check(rule, msg, cond, detail=""):
    if cond:
        ok(rule, msg)
    else:
        bad(rule, "%s: %s" % (msg, detail))


# ---- what the browser saw (verbose mode only: added to a failing browser rule's FAIL line) ----
# While a browser check runs, the page's JavaScript errors and its failed /api calls are recorded.
# A FAIL line then ends with what happened in the page since the previous rule's result, so a broken
# script or a 4xx/5xx from the API is named instead of only "element not found".
DIAG = {"on": False, "errors": [], "api": [], "mark": (0, 0)}


def watch(page):
    DIAG.update(on=True, errors=[], api=[], mark=(0, 0))

    def on_error(e):
        DIAG["errors"].append(str(e).split("\n")[0][:140])

    def on_console(m):
        if m.type == "error" and not m.text.startswith("Failed to load resource"):
            DIAG["errors"].append("console.error: " + m.text.split("\n")[0][:120])

    def on_response(r):
        if "/api/" in r.url and r.status >= 400:
            DIAG["api"].append("%s %s -> %d" % (r.request.method, r.url.split("//", 1)[-1].split("/", 1)[-1].split("?")[0], r.status))

    def on_failed(r):
        if "/api/" in r.url:
            DIAG["api"].append("%s /%s failed (%s)" % (r.method, r.url.split("//", 1)[-1].split("/", 1)[-1].split("?")[0], r.failure))
    page.on("pageerror", on_error)
    page.on("console", on_console)
    page.on("response", on_response)
    page.on("requestfailed", on_failed)


def unwatch():
    DIAG["on"] = False


def page_notes():
    """What the page did since the previous rule's result; empty if nothing went wrong."""
    if not DIAG["on"]:
        return ""
    e0, a0 = DIAG["mark"]
    errs, api = DIAG["errors"][e0:], DIAG["api"][a0:]
    DIAG["mark"] = (len(DIAG["errors"]), len(DIAG["api"]))
    parts = []
    if errs:
        parts.append("page JS error%s: %s" % ("s (%d, first)" % len(errs) if len(errs) > 1 else "", errs[0]))
    if api:
        parts.append("API calls that failed: " + ", ".join(api[-3:]))
    return (" [" + "; ".join(parts) + "]") if parts else ""


# ---- HTTP ----
def request(method, url, data=None, token=None, headers=None, timeout=5):
    left = DEADLINE - (time.time() - T0)
    if left <= 0:
        return 0, b"", {}
    timeout = min(timeout, max(left, 0.5))
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except Exception:
            raw = b""
        return e.code, raw, e.headers
    except Exception:
        return 0, b"", {}


def call(method, url, body=None, token=None, timeout=5):
    data = None if body is None else json.dumps(body).encode()
    st, raw, _ = request(method, url, data, token, {"Content-Type": "application/json"} if data else {}, timeout)
    try:
        return st, (json.loads(raw) if raw else None)
    except Exception:
        return st, None


def dig(d, path):
    for k in path.split("."):
        if isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return None
    return d


def sid(x):
    return None if x is None else str(x)


def short(d, n=200):
    return json.dumps(d)[:n]


ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|\+00:00)$")


def healthy():
    return all(call("GET", n + "/api/health", timeout=2)[0] == 200 for n in NODES)


def boot():
    if not healthy():
        try:
            log = open("/tmp/start.log", "w")
            subprocess.Popen(["/app/start.sh"], cwd="/app", stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True)
        except OSError as e:
            say("FAIL 0 could not start /app/start.sh: %s" % e)
        for _ in range(15):
            if healthy():
                break
            time.sleep(1)
    if not healthy():
        say("FAIL 0 cluster is not answering /api/health on 8000, 8001, 8002 (see /tmp/start.log)")
        print("RESULT: 0 passed, 1 failed")
        sys.exit(1)


def pids_on(port):
    try:
        out = subprocess.run(["ss", "-ltnpH", "sport = :%d" % port], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return []
    return sorted({int(p) for p in re.findall(r"pid=(\d+)", out)} - {os.getpid(), os.getppid(), 1})


# ---- basics ----
def reg(label):
    name = "%s%s" % (label, RUN)
    st, d = call("POST", N0 + "/api/auth/register", {"username": name, "password": PW})
    return {"name": name, "token": dig(d, "token"), "id": dig(d, "user.id")}


def users(*labels):
    out = [reg(l) for l in labels]
    if not all(u["token"] for u in out):
        raise RuntimeError("setup: could not register users (register must work first)")
    return out


def mkws(slug, u):
    st, d = call("POST", N0 + "/api/workspaces", {"slug": slug, "name": "Edge " + slug}, u["token"])
    return dig(d, "general_channel.id")


def newchan(slug, name, u):
    return dig(call("POST", "%s/api/workspaces/%s/channels" % (N0, slug), {"name": name, "is_private": False}, u["token"])[1], "channel.id")


def join(cid, u):
    return call("POST", "%s/api/channels/%s/join" % (N0, cid), {}, u["token"])[0]


def post(cid, u, body, node=N0):
    return call("POST", "%s/api/channels/%s/messages" % (node, cid), {"body": body}, u["token"])


def listing(cid, u, node=N0):
    st, d = call("GET", "%s/api/channels/%s/messages?limit=50" % (node, cid), None, u["token"])
    return st, [m for m in ((d or {}).get("messages") or []) if isinstance(m, dict)]


def find_msg(items, mid):
    for m in items or []:
        if sid(m.get("id")) == sid(mid):
            return m
    return None


# ---- WebSocket ----
async def ws_open(port, token):
    return await websockets.connect("ws://127.0.0.1:%d/api/ws?token=%s" % (port, token), open_timeout=3)


async def wait_for(ws, pred, timeout):
    end = time.time() + timeout
    while True:
        left = end - time.time()
        if left <= 0:
            return None
        try:
            f = json.loads(await asyncio.wait_for(ws.recv(), left))
        except Exception:
            return None
        if isinstance(f, dict) and pred(f):
            return f


async def subscribe(ws, cid, timeout=3):
    await ws.send(json.dumps({"type": "subscribe", "channel_id": cid}))
    f = await wait_for(ws, lambda f: f.get("type") == "subscribed" and sid(f.get("channel_id")) == sid(cid), timeout)
    return (f or {}).get("head_event_id")


async def durable(ws, cid, n, timeout, after):
    got, end = [], time.time() + timeout
    while len(got) < n and time.time() < end:
        f = await wait_for(ws, lambda f: sid(f.get("channel_id")) == sid(cid) and isinstance(f.get("event_id"), int)
                           and f["event_id"] > after, end - time.time())
        if f is None:
            break
        got.append(f)
    return got


async def in_thread(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def run_async(rules, coro_fn, need_ws=True):
    if need_ws and websockets is None:
        for r in rules:
            bad(r, "the python `websockets` package is not available to the check")
        return
    try:
        asyncio.run(asyncio.wait_for(coro_fn(), max(1, DEADLINE - (time.time() - T0))))
    except Exception as e:
        for r in rules:
            bad(r, "error or timeout (%s %s)" % (type(e).__name__, e))


# ---- IRC client ----
class Irc:
    def __init__(self):
        self.lines, self.r, self.w = [], None, None

    async def connect(self):
        self.r, self.w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", 6667), 3)
        return self

    def send(self, line):
        self.w.write((line + "\r\n").encode())

    async def expect(self, pred, timeout=3):
        """Returns the first new line matching pred (lines read are kept in self.lines), or None."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(self.r.readline(), end - time.time())
            except Exception:
                return None
            if not raw:
                return None  # closed
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("PING "):
                self.send("PONG " + line[5:])
            self.lines.append(line)
            if pred(line):
                return line
        return None

    async def closed(self, timeout=2):
        end = time.time() + timeout
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(self.r.readline(), end - time.time())
            except Exception:
                return False
            if not raw:
                return True
            self.lines.append(raw.decode("utf-8", "replace").rstrip("\r\n"))
        return False

    def close(self):
        try:
            self.w.close()
        except Exception:
            pass


def numeric(line):
    p = line.split(" ")
    return p[1] if len(p) > 1 and line.startswith(":") else (p[0] if p else "")


async def irc_login(u, nick=None):
    c = await Irc().connect()
    nick = nick or u["name"]
    c.send("PASS " + u["token"])
    c.send("NICK " + nick)
    c.send("USER %s 0 * :%s" % (nick, nick))
    await c.expect(lambda l: numeric(l) in ("422", "376", "464", "433"), 4)
    return c


# ===== E.1 Message edit and delete (rules 1-9) =====
def stage1():
    o, a, b = users("eo", "ea", "eb")
    S = "e1-" + RUN
    gen = mkws(S, o)
    for u in (a, b):
        join(gen, u)
    C = newchan(S, "edits", o)
    for u in (a, b):
        join(C, u)
    mid = dig(post(C, a, "first draft")[1], "message.id")
    st, d = call("PATCH", "%s/api/messages/%s" % (N0, mid), {"body": "final text"}, a["token"])
    m = dig(d, "message") or {}
    check(1, "PATCH /api/messages/{id} by the author returns 200 {message} with the new body and edited_at set",
          st == 200 and m.get("body") == "final text" and ISO.match(str(m.get("edited_at"))) is not None,
          "status %s, message %s" % (st, short(m)))
    _, lst = listing(C, b, N2)
    lm = find_msg(lst, mid) or {}
    check(2, "the listing on 8002 shows the edited body and edited_at", lm.get("body") == "final text" and lm.get("edited_at"),
          "listed %s" % short(lm, 150))
    got = [call("PATCH", "%s/api/messages/%s" % (N0, mid), {"body": "hijack"}, b["token"])[0],
           call("PATCH", "%s/api/messages/%s" % (N0, mid), {"body": "hijack"}, o["token"])[0],
           call("PATCH", "%s/api/messages/99999999" % N0, {"body": "x"}, a["token"])[0],
           call("PATCH", "%s/api/messages/%s" % (N0, mid), {"body": "  "}, a["token"])[0],
           call("PATCH", "%s/api/messages/%s" % (N0, mid), {"body": "x"})[0]]
    check(3, "edit by another member 403, by the workspace owner 403, unknown id 404, blank body 400, no token 401",
          got == [403, 403, 404, 400, 401], "got %s" % got)

    async def r4_9():
        ws = await ws_open(8001, b["token"])
        try:
            h = await subscribe(ws, C)
            if not isinstance(h, int):
                for r in (4, 9):
                    bad(r, "subscribe gave no head_event_id")
                return
            await in_thread(lambda: call("PATCH", "%s/api/messages/%s" % (N0, mid), {"body": "live edit"}, a["token"]))
            got = await durable(ws, C, 1, 3, h)
            f = got[0] if got else {}
            check(4, "an edit emits message.updated with the next event_id and the updated message",
                  f.get("type") == "message.updated" and f.get("event_id") == h + 1 and dig(f, "message.body") == "live edit",
                  "head %s, frame %s" % (h, short(f)))
            s1, d1 = await in_thread(lambda: call("DELETE", "%s/api/messages/%s" % (N0, mid), None, a["token"]))
            s2, d2 = await in_thread(lambda: call("DELETE", "%s/api/messages/%s" % (N1, mid), None, a["token"]))
            check(5, "DELETE by the author returns 200 {deleted: true}, and again 200 {deleted: true}",
                  (s1, s2) == (200, 200) and d1 == {"deleted": True} and d2 == {"deleted": True},
                  "got %s %s and %s %s" % (s1, d1, s2, d2))
            got = await durable(ws, C, 2, 2, h + 1)
            kinds = [(f.get("type"), f.get("event_id")) for f in got]
            check(9, "a delete emits one message.deleted with the next event_id (the repeat emits nothing)",
                  kinds == [("message.deleted", h + 2)] and sid(got[0].get("message_id") or dig(got[0], "message.id")) == sid(mid),
                  "head %s, frames %s" % (h, [short(f, 120) for f in got]))
        finally:
            await ws.close()
    run_async([4, 5, 9], r4_9)
    _, lst = listing(C, a)
    check(6, "a deleted message no longer appears in the listing", find_msg(lst, mid) is None,
          "still listed: %s" % short(find_msg(lst, mid), 150))
    s = call("PATCH", "%s/api/messages/%s" % (N0, mid), {"body": "zombie"}, a["token"])[0]
    check(7, "editing a deleted message returns 409", s == 409, "got %s" % s)
    m2 = dig(post(C, a, "owner removes this")[1], "message.id")
    got = [call("DELETE", "%s/api/messages/%s" % (N0, m2), None, b["token"])[0],
           call("DELETE", "%s/api/messages/%s" % (N0, m2), None, o["token"])[0],
           call("DELETE", "%s/api/messages/99999999" % N0, None, a["token"])[0]]
    check(8, "DELETE by another member 403, by the workspace owner 200, unknown id 404", got == [403, 200, 404], "got %s" % got)


# ===== E.2 IRC registration and commands (rules 10-17) =====
def stage2():
    a, b = users("ia", "ib")

    async def body2():
        try:
            c = await Irc().connect()
        except Exception as e:
            for r in STAGES[2]:
                bad(r, "could not connect to 127.0.0.1:6667 (%s)" % type(e).__name__)
            return
        c.send("PASS " + a["token"])
        c.send("NICK " + a["name"])
        c.send("USER %s 0 * :%s" % (a["name"], a["name"]))
        await c.expect(lambda l: numeric(l) in ("422", "464"), 4)
        nums = [numeric(l) for l in c.lines if numeric(l).isdigit()]
        check(10, "PASS <token>, NICK, USER give numerics 001 to 005, then 422", nums[:6] == ["001", "002", "003", "004", "005", "422"],
              "numerics %s" % nums[:8])
        d = await Irc().connect()
        d.send("PASS not-a-real-token-" + RUN)
        d.send("NICK zz" + RUN)
        d.send("USER zz 0 * :zz")
        l = await d.expect(lambda l: numeric(l) in ("464", "001"), 3)
        check(11, "a bad PASS token gets 464 and no 001", l is not None and numeric(l) == "464", "got %r" % l)
        d.close()
        e = await Irc().connect()
        e.send("PASS " + b["token"])
        e.send("NICK " + a["name"])  # taken by c
        e.send("USER %s 0 * :x" % b["name"])
        l1 = await e.expect(lambda l: numeric(l) in ("433", "001"), 3)
        e.send("NICK " + b["name"])
        l2 = await e.expect(lambda l: numeric(l) in ("001", "422"), 3)
        check(12, "NICK already in use gets 433, the connection stays open, and a free NICK then registers",
              l1 is not None and numeric(l1) == "433" and l2 is not None, "first %r, then %r" % (l1, l2))
        c.send("PING :tok" + RUN)
        l = await c.expect(lambda l: l.split(" ")[0] == "PONG" or " PONG " in l, 2)
        check(13, "PING :x is answered with PONG carrying x", l is not None and ("tok" + RUN) in l, "got %r" % l)
        c.send("FOOBAR something")
        l = await c.expect(lambda l: numeric(l) == "421", 2)
        check(14, "an unknown command gets 421", l is not None, "no 421 (last lines %s)" % c.lines[-3:])
        before = len(c.lines)
        for cmd in ("NAMES", "WHO " + a["name"], "LIST", "TOPIC", "MODE " + a["name"], "PONG :x"):
            c.send(cmd)
        await c.expect(lambda l: False, 1.2)
        bad421 = [l for l in c.lines[before:] if numeric(l) == "421"]
        check(15, "NAMES, WHO, LIST, TOPIC, MODE and PONG are accepted (no 421)", not bad421, "got %s" % bad421)
        await e.expect(lambda l: False, 0.3)  # let the rest of registration (such as 422) arrive first
        quit_at = len(e.lines)
        e.send("QUIT :bye")
        closed = await e.closed(2)
        errs = [l for l in e.lines[quit_at:] if numeric(l)[:1] in ("4", "5") and numeric(l).isdigit() or l.startswith("ERROR")]
        errs = [l for l in errs if numeric(l) not in ("433", "422")]
        check(16, "QUIT closes the connection without an error reply", closed and not errs,
              "closed %s, error lines %s" % (closed, errs[-2:]))
        f = await Irc().connect()
        f.send("NICK nopass" + RUN)
        f.send("USER nopass 0 * :x")
        l = await f.expect(lambda l: numeric(l) in ("464", "451", "001"), 3)
        check(17, "registering without PASS gets 464 (and no 001)", l is not None and numeric(l) == "464", "got %r" % l)
        f.close()
        c.close()
    run_async(list(STAGES[2]), body2, need_ws=False)


# ===== E.3 IRC channels and bridging (rules 18-25) =====
def stage3():
    o, a, b = users("bo", "ba", "bb")
    S = "e3-" + RUN
    gen = mkws(S, o)
    for u in (a, b):
        join(gen, u)
    IRCNAME = "#%s/general" % S

    async def body3():
        try:
            ca = await irc_login(a)
            cb = await irc_login(b)
        except Exception as e:
            for r in STAGES[3]:
                bad(r, "could not connect to 127.0.0.1:6667 (%s)" % type(e).__name__)
            return
        n0 = len(ca.lines)
        ca.send("JOIN " + IRCNAME)
        await ca.expect(lambda l: numeric(l) in ("366", "403"), 3)
        seq = [l for l in ca.lines[n0:] if (" JOIN " in l and l.startswith(":" + a["name"] + "!")) or numeric(l) in ("353", "366", "403")]
        kinds = ["JOIN" if " JOIN " in l else numeric(l) for l in seq]
        names = " ".join(l.split(" :", 1)[-1] for l in seq if numeric(l) == "353").split()
        check(18, "JOIN #slug/general gets the joiner's own JOIN line, then 353 (listing the joiner's nick), then 366",
              kinds[:3] == ["JOIN", "353", "366"] and a["name"] in [n.lstrip("@+") for n in names],
              "got %s, nicks in 353 %s" % ([l[:90] for l in seq[:4]], names[:6]))
        cb.send("JOIN " + IRCNAME)
        await cb.expect(lambda l: numeric(l) == "366", 3)
        got = []
        for name in ("#%s-general" % S, "#%s/nosuch" % S):
            ca.send("JOIN " + name)
            l = await ca.expect(lambda l: numeric(l) in ("403", "366"), 2)
            got.append(numeric(l) if l else None)
        check(19, "JOIN #slug-general (hyphen) and an unknown channel name both get 403", got == ["403", "403"], "got %s" % got)
        ws = await ws_open(8002, o["token"])
        try:
            h = await subscribe(ws, gen)
            body = "from web " + RUN
            await in_thread(post, gen, o, body, N0)
            pat = re.compile(r"^:%s!\S+@\S+ PRIVMSG %s :%s$" % (re.escape(o["name"]), re.escape(IRCNAME), re.escape(body)))
            l = await ca.expect(lambda l: pat.match(l) is not None, 3)
            check(20, "a web post reaches IRC as :<username>!<user>@<host> PRIVMSG #slug/general :<body>", l is not None,
                  "no matching line; last lines %s" % ca.lines[-2:])
            ca.send("PRIVMSG %s :from irc %s" % (IRCNAME, RUN))
            fr = await durable(ws, gen, 2, 3, h)
            lb = await cb.expect(lambda l: ("PRIVMSG %s :from irc %s" % (IRCNAME, RUN)) in l, 3)
            _, lst = listing(gen, o, N1)
            hit = [m for m in lst if m.get("body") == "from irc " + RUN]
            check(21, "an IRC PRIVMSG is stored as a message by that user and is listed on the web",
                  len(hit) == 1 and sid(hit[0].get("author_id")) == sid(a["id"]), "listed %s" % short(hit, 150))
            check(22, "the IRC message reaches web sockets as message.created with the next event_id",
                  [(f.get("type"), f.get("event_id")) for f in fr] == [("message.created", h + 1), ("message.created", h + 2)],
                  "head %s, frames %s" % (h, [(f.get("type"), f.get("event_id")) for f in fr]))
            echo = [x for x in ca.lines if ("PRIVMSG %s :from irc %s" % (IRCNAME, RUN)) in x]
            check(23, "the IRC message reaches other IRC clients in the channel, and is not echoed to its sender",
                  lb is not None and lb.startswith(":%s!" % a["name"]) and not echo, "other client got %r, sender echo %s" % (lb, echo))
            for i in range(3):
                await in_thread(post, gen, o, "mix web %d %s" % (i, RUN), NODES[i % 3])
                ca.send("PRIVMSG %s :mix irc %d %s" % (IRCNAME, i, RUN))
                await asyncio.sleep(0.15)
            fr = await durable(ws, gen, 6, 4, h + 2)
            ids = [f.get("event_id") for f in fr]
            check(24, "web and IRC writers alternating give 6 consecutive event_ids", ids == list(range(h + 3, h + 9)),
                  "head+2 %s, ids %s" % (h + 2, ids))
            pids = pids_on(8001)
            irc_pids = set(pids_on(6667))
            if not pids or set(pids) & irc_pids:
                bad(25, "the IRC gateway must be its own process, not inside the node on 8001 (PIDs 8001 %s, 6667 %s)" % (pids, sorted(irc_pids)))
                return
            for p in pids:
                os.kill(p, 9)
            await asyncio.sleep(0.5)
            b2 = "while down " + RUN
            await in_thread(post, gen, o, b2, N0)
            l1 = await ca.expect(lambda l: l.endswith(":" + b2), 3)
            ca.send("PRIVMSG %s :irc while down %s" % (IRCNAME, RUN))
            await asyncio.sleep(1)
            _, lst = listing(gen, o, N2)
            stored = any(m.get("body") == "irc while down " + RUN for m in lst)
            check(25, "with the node on 8001 killed, web-to-IRC and IRC-to-web bridging still work", l1 is not None and stored,
                  "web->IRC %s, IRC->web stored %s" % (l1 is not None, stored))
            t = time.time()
            while time.time() - t < 15 and time.time() - T0 < DEADLINE - 1:
                if call("GET", N1 + "/api/health", timeout=1)[0] == 200:
                    break
                await asyncio.sleep(0.5)
            else:
                say("NOTE: node 1 did not come back on 8001; /app/start.sh must restart a killed node (Cluster.2)")
        finally:
            await ws.close()
            ca.close()
            cb.close()
    run_async(list(STAGES[3]), body3)


# ===== E.4 The UI's served HTML (rules 26-30) =====
ALWAYS = ("auth-form auth-submit auth-toggle workspace-create-form empty-state-create-workspace join-workspace-form "
          "join-workspace-submit channel-list dms-list new-channel-btn current-user logout-btn workspace-header "
          "workspace-settings-btn workspace-settings-close workspace-name-input workspace-join-mode workspace-general-submit "
          "channel-title channel-topic channel-settings-btn channel-settings-title channel-settings-close channel-members-list "
          "channel-add-member-form channel-add-member-input channel-add-member-submit channel-topic-input channel-topic-submit "
          "archive-channel-btn unarchive-channel-btn create-channel-form create-channel-private create-channel-submit "
          "create-channel-cancel message-list message-input send-btn thread-panel thread-input thread-send close-thread "
          "settings-tab-members settings-tab-invitations settings-tab-general settings-pane-members settings-pane-invitations "
          "settings-pane-general create-invitation-btn create-invitation-form create-invitation-submit invitations-list").split()
IDS = ["auth-modal", "create-channel-modal", "channel-settings-modal", "workspace-settings-modal", "create-invitation-form"]
ROLES = {
    "primary": ("auth-submit send-btn thread-send create-channel-submit join-workspace-submit workspace-general-submit "
                "create-invitation-submit channel-topic-submit channel-add-member-submit").split(),
    "danger": ["archive-channel-btn", "logout-btn"],
    "secondary": ("new-channel-btn create-invitation-btn unarchive-channel-btn create-channel-cancel close-thread "
                  "channel-settings-close workspace-settings-close channel-settings-btn workspace-settings-btn").split(),
}
COLORS = {"primary": "rgb(0, 122, 90)", "danger": "rgb(224, 30, 90)", "secondary": "rgb(18, 100, 163)"}


def tags_with(html, attr, value):
    return re.findall(r"<[a-zA-Z][^>]*\b%s\s*=\s*[\"']%s[\"'][^>]*>" % (attr, re.escape(value)), html)


def stage4():
    pages = []
    for n in NODES:
        st, raw, hd = request("GET", n + "/")
        pages.append((st, (hd.get("Content-Type") or "") if hd else "", raw.decode("utf-8", "replace")))
    check(26, "GET / returns 200 text/html on 8000, 8001 and 8002",
          all(st == 200 and ct.startswith("text/html") and "<html" in h.lower() for st, ct, h in pages),
          "got %s" % [(st, ct) for st, ct, _ in pages])
    html = pages[0][2]
    missing = [t for t in ALWAYS if not tags_with(html, "data-testid", t)]
    check(27, "every always-present data-testid is in the initial HTML", not missing, "missing %s" % missing)
    mids = [i for i in IDS if not tags_with(html, "id", i)]
    check(28, "the DOM ids auth-modal, create-channel-modal, channel-settings-modal, workspace-settings-modal, create-invitation-form are present",
          not mids, "missing %s" % mids)
    wrong = []
    for role, tids in ROLES.items():
        for t in tids:
            tags = tags_with(html, "data-testid", t)
            if not tags or not re.search(r"\bdata-button-role\s*=\s*[\"']%s[\"']" % role, tags[0]):
                wrong.append(t)
    check(29, "each pinned button carries the right data-button-role on the same element as its data-testid", not wrong,
          "wrong or missing for %s" % wrong)
    refs = re.findall(r"<script[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", html) + \
        re.findall(r"<link[^>]*\brel\s*=\s*[\"']stylesheet[\"'][^>]*\bhref\s*=\s*[\"']([^\"']+)[\"']", html)
    local = [r for r in refs if not re.match(r"^(https?:)?//", r)]
    remote = [r for r in refs if re.match(r"^(https?:)?//", r)]
    broken = [r for r in local if request("GET", urllib.parse.urljoin(N1 + "/", r))[0] != 200]
    check(30, "every script and stylesheet the page loads is served locally (200 on 8001), nothing from the internet",
          not broken and not remote, "broken %s, remote %s" % (broken, remote))


# ===== E.5 The UI in a browser (rules 31-37) =====
def stage5():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        for r in STAGES[5]:
            bad(r, "playwright is not available to the check (/opt/test-venv)")
        return
    o = users("uo")[0]
    with sync_playwright() as p:
        br = p.chromium.launch(args=["--no-sandbox"])
        try:
            page = br.new_page(viewport={"width": 1280, "height": 900})
            page.set_default_timeout(3000)
            watch(page)
            page.goto(N0 + "/")
            page.mouse.move(1279, 899)
            res = page.evaluate("""(roles) => {
                const out = [];
                for (const [role, ids] of Object.entries(roles)) for (const id of ids) {
                    const el = document.querySelector('[data-testid="' + id + '"]');
                    if (!el) { out.push([id, 'missing', '']); continue; }
                    const cs = getComputedStyle(el);
                    out.push([id, cs.backgroundColor, cs.color, role]);
                } return out; }""", ROLES)
            off = ["%s: %s on %s" % (r[0], r[2], r[1]) for r in res if r[1] != COLORS.get(r[3]) or r[2] != "rgb(255, 255, 255)"]
            check(31, "pinned buttons have their exact resting colours (primary #007a5a, danger #e01e5a, secondary #1264a3, white text)",
                  not off, "%d wrong: %s" % (len(off), off[:4]))

            def token_of():
                return page.evaluate("() => localStorage.getItem('huddle.token') || localStorage.getItem('token') || "
                                     "localStorage.getItem('auth_token') || window.__huddle_token || null")
            form = '[data-testid="auth-form"]'
            page.fill(form + ' input[name="username"]', "bad name " + RUN)
            page.fill(form + ' input[name="password"]', "short")
            page.click('[data-testid="auth-submit"]')
            page.wait_for_timeout(700)
            err = page.evaluate("""() => { const f = document.querySelector('[data-testid="auth-form"]');
                const a = f && f.querySelector('[role="alert"]');
                return a && a.offsetParent !== null ? a.innerText.trim() : ''; }""")
            check(32, "an invalid sign-up shows a visible error ([role=alert] in auth-form) and stores no token",
                  bool(err) and not token_of(), "alert text %r, token %r" % (err, token_of()))
            name = "ui" + RUN
            page.fill(form + ' input[name="username"]', name)
            page.fill(form + ' input[name="password"]', PW)
            page.click('[data-testid="auth-submit"]')
            try:
                page.wait_for_function("() => localStorage.getItem('huddle.token') || window.__huddle_token", timeout=3000)
            except Exception:
                pass
            tok = token_of()
            me = dig(call("GET", N0 + "/api/auth/me", None, tok)[1], "user.username") if tok else None
            cu = page.inner_text('[data-testid="current-user"]') if me else ""
            seen = page.evaluate("""() => { const f = document.querySelector('[data-testid="auth-form"]');
                const a = f && f.querySelector('[role="alert"]');
                return [f ? f.getAttribute('data-mode') : 'no auth-form', a && a.offsetParent !== null ? a.innerText.trim() : '']; }""")
            check(33, "a valid sign-up logs in: huddle.token holds a working token and current-user shows the username",
                  me == name and name in cu, "token works for %r, current-user %r; after submit the form's data-mode is %r%s" %
                  (me, cu, seen[0], ", alert %r" % seen[1][:80] if seen[1] else "")
                  + ("" if seen[0] != "login" else " (the page must open in register mode, so this click tried to log in)"))
            if me != name:
                for r in range(34, 38):
                    bad(r, "not checked: the sign-up in rule 33 did not log in")
                return
            u = {"name": name, "token": tok, "id": dig(call("GET", N0 + "/api/auth/me", None, tok)[1], "user.id")}
            S = "e5-" + RUN
            gen = mkws(S, u)
            page.reload()
            page.wait_for_selector('[data-testid="channel-entry"][data-channel-name="general"]', timeout=4000)
            ent = page.query_selector('[data-testid="channel-entry"][data-channel-name="general"]')
            txt = ent.inner_text() if ent else ""
            page.click('[data-testid="channel-entry"][data-channel-name="general"]')
            page.wait_for_timeout(500)
            title = page.inner_text('[data-testid="channel-title"]')
            check(34, "after a reload the sidebar lists #general (data-channel-id, data-channel-name) and clicking it sets channel-title",
                  ent is not None and sid(ent.get_attribute("data-channel-id")) == sid(gen) and "#" in txt and "general" in title,
                  "entry text %r, data-channel-id %r (want %s), title %r" % (txt, ent and ent.get_attribute("data-channel-id"), gen, title))
            body = "hello from the ui " + RUN
            page.fill('[data-testid="message-input"]', body)
            page.click('[data-testid="send-btn"]')
            try:
                page.wait_for_selector('[data-testid="message-body"]:has-text("%s")' % body, timeout=3000)
                shown = True
            except Exception:
                shown = False
            _, lst = listing(gen, u)
            page.reload()
            page.click('[data-testid="channel-entry"][data-channel-name="general"]')
            try:
                page.wait_for_selector('[data-testid="message-body"]:has-text("%s")' % body, timeout=3000)
                kept = True
            except Exception:
                kept = False
            check(35, "posting with message-input and send-btn shows the message, stores it, and it survives a reload",
                  shown and any(m.get("body") == body for m in lst) and kept,
                  "shown %s, stored %s, after reload %s" % (shown, any(m.get("body") == body for m in lst), kept))
            n_before = len(listing(gen, u)[1])
            page.fill('[data-testid="message-input"]', "   ")
            page.click('[data-testid="send-btn"]')
            page.wait_for_timeout(700)
            n_after = len(listing(gen, u)[1])
            check(36, "sending an empty or blank message posts nothing", n_after == n_before, "messages %s -> %s" % (n_before, n_after))
            join(gen, o)
            live = "live from another user " + RUN
            post(gen, o, live, N2)
            try:
                page.wait_for_selector('[data-testid="message-body"]:has-text("%s")' % live, timeout=3000)
                arrived = True
            except Exception:
                arrived = False
            check(37, "a message another user posts (on 8002) appears in the open channel within 3s, without a reload", arrived,
                  "not shown after 3s (the UI must use the WebSocket)")
        except Exception as e:
            for r in STAGES[5]:
                bad(r, "check stopped (%s)%s" % (str(e).split("\n")[0][:160], _waiting_for(e)))
        finally:
            unwatch()
            br.close()


# ---- browser helpers for stages 6 and 7 ----
def T(tid):
    return '[data-testid="%s"]' % tid


def visible(loc, timeout=2500):
    try:
        loc.first.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


def gone(loc, timeout=2500):
    try:
        loc.first.wait_for(state="detached", timeout=timeout)
        return True
    except Exception:
        return loc.count() == 0 or not loc.first.is_visible()


def _waiting_for(e):
    """The element a Playwright timeout was waiting for, and why it could not use it."""
    txt = str(e)
    m = re.search(r"waiting for (locator\(.*?\)|get_by_\w+\(.*?\))(?:\n|$)", txt)
    loc = m.group(1) if m else ""
    out = " (waiting for %s" % (loc if len(loc) <= 140 else "..." + loc[-137:]) if m else ""
    if not out:
        return ""
    if "not visible" in txt:
        out += ": it exists but is not visible; hover toolbars must show on mouse hover"
    elif "not enabled" in txt or "disabled" in txt:
        out += ": it is disabled"
    elif "intercepts pointer events" in txt:
        out += ": another element covers it"
    elif "resolved to" not in txt:
        out += ": no element matches"
    return out + ")"


def step(rule, fn):
    """Runs one browser rule; a missing element or a timeout fails only this rule."""
    if time.time() - T0 > DEADLINE - 1:
        bad(rule, "not reached: the check's time budget ran out")
        return
    try:
        fn()
    except Exception as e:
        bad(rule, "browser step failed: %s%s" % (str(e).split("\n")[0][:150], _waiting_for(e)))


def with_browser(rules, fn):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        for r in rules:
            bad(r, "playwright is not available to the check (/opt/test-venv)")
        return
    with sync_playwright() as p:
        br = p.chromium.launch(args=["--no-sandbox"])
        try:
            page = br.new_page(viewport={"width": 1280, "height": 900})
            page.set_default_timeout(2500)
            watch(page)
            dialogs = []

            def on_dialog(d):
                dialogs.append(d.type)
                d.accept()
            page.on("dialog", on_dialog)
            try:
                fn(page, dialogs)
            except Exception as e:  # caught here, while the page is still watched, so its errors are named
                for r in rules:
                    bad(r, "check stopped (%s)%s" % (str(e).split("\n")[0][:160], _waiting_for(e)))
        finally:
            unwatch()
            br.close()


def open_channel(page, name):
    page.click('%s[data-channel-name="%s"]' % (T("channel-entry"), name))
    page.wait_for_timeout(300)


def row(page, text):
    return page.locator(T("message"), has_text=text).first


def ui_token(page):
    return page.evaluate("() => localStorage.getItem('huddle.token') || window.__huddle_token || null")


# ===== E.6 Message actions in the browser (rules 38-45) =====
def stage6():
    u, o = users("ma", "mo")
    S = "e6-" + RUN
    gen = mkws(S, u)
    join(gen, o)
    quiet = newchan(S, "quiet", u)
    ids = {b: dig(post(gen, u, b + " " + RUN)[1], "message.id") for b in ("edit me", "delete me", "thread me", "react me")}

    def run(page, dialogs):
        page.add_init_script("localStorage.setItem('huddle.token', %s)" % json.dumps(u["token"]))
        page.goto(N0 + "/")
        page.wait_for_selector('%s[data-channel-name="general"]' % T("channel-entry"), timeout=4000)
        open_channel(page, "general")

        def r38():
            r = row(page, "edit me " + RUN)
            r.hover()
            r.locator(T("edit-message-btn")).click()
            inp = r.locator(T("edit-message-input"))
            inp.fill("edited by ui " + RUN)
            inp.press("Enter")
            shown = visible(page.locator(T("message-body"), has_text="edited by ui " + RUN))
            ind = visible(row(page, "edited by ui " + RUN).locator(T("edited-indicator")))
            api = (find_msg(listing(gen, u)[1], ids["edit me"]) or {}).get("body")
            page.reload()
            open_channel(page, "general")
            kept = visible(row(page, "edited by ui " + RUN).locator(T("edited-indicator")))
            check(38, "hover a message, Edit, type, Enter: the new body shows with (edited), is saved, and survives a reload",
                  shown and ind and api == "edited by ui " + RUN and kept,
                  "shown %s, indicator %s, API body %r, after reload %s" % (shown, ind, api, kept))
        step(38, r38)

        def r39():
            r = row(page, "delete me " + RUN)
            r.hover()
            r.locator(T("delete-message-btn")).click()
            g = gone(page.locator(T("message"), has_text="delete me " + RUN))
            api = find_msg(listing(gen, u)[1], ids["delete me"]) is None
            page.reload()
            open_channel(page, "general")
            still = page.locator(T("message"), has_text="delete me " + RUN).count()
            check(39, "hover a message, Delete: the row disappears, the API no longer lists it, and it stays gone after a reload",
                  g and api and still == 0, "row gone %s, deleted in API %s, rows after reload %s" % (g, api, still))
        step(39, r39)

        def r40():
            r = row(page, "thread me " + RUN)
            r.hover()
            r.locator(T("open-thread-btn")).click()
            panel = page.locator(T("thread-panel"))
            pv = visible(panel)
            page.fill(T("thread-input"), "reply in ui " + RUN)
            page.click(T("thread-send"))
            inpanel = visible(panel.locator("text=reply in ui " + RUN))
            rc = visible(row(page, "thread me " + RUN).locator(T("reply-count"), has_text="1 repl"))
            reps = (call("GET", "%s/api/messages/%s/replies" % (N0, ids["thread me"]), None, u["token"])[1] or {}).get("replies") or []
            pb, mb = panel.bounding_box(), page.locator(T("message-list")).bounding_box()
            right = bool(pb and mb and pb["x"] >= mb["x"] + mb["width"] - 5)
            check(40, "open-thread-btn opens thread-panel on the right; a reply sent there shows in it, is saved, and the parent shows 1 reply",
                  pv and inpanel and rc and right and [x.get("body") for x in reps] == ["reply in ui " + RUN],
                  "panel %s (right of messages %s), reply shown %s, reply-count %s, API replies %s" % (pv, right, inpanel, rc, [x.get("body") for x in reps]))
        step(40, r40)

        def r41():
            page.click(T("close-thread"))
            check(41, "close-thread hides the thread panel", gone(page.locator(T("thread-panel")).locator("visible=true")) or not page.locator(T("thread-panel")).is_visible(),
                  "thread-panel still visible")
        step(41, r41)

        def r42():
            r = row(page, "react me " + RUN)
            r.hover()
            r.locator(T("reaction-button")).click()
            picker = page.locator(T("emoji-picker"))
            pv = visible(picker)
            n = picker.locator(T("emoji-option")).count()
            picker.locator(T("emoji-option")).first.click()
            chip = row(page, "react me " + RUN).locator(T("reaction-chip"))
            cv = visible(chip)
            ctext = chip.first.inner_text() if cv else ""
            rs = (find_msg(listing(gen, u)[1], ids["react me"]) or {}).get("reactions") or []
            check(42, "reaction-button opens an emoji-picker grid (6+ emoji-option, no prompt); picking one shows a reaction-chip with count 1 and saves it",
                  pv and n >= 6 and "prompt" not in dialogs and cv and "1" in ctext and len(rs) == 1 and rs[0].get("count") == 1,
                  "picker %s with %d options, dialogs %s, chip %r, API reactions %s" % (pv, n, dialogs, ctext, short(rs, 120)))
        step(42, r42)

        def r43():
            chip = row(page, "react me " + RUN).locator(T("reaction-chip"))
            chip.first.click()
            g = gone(chip)
            rs = (find_msg(listing(gen, u)[1], ids["react me"]) or {}).get("reactions") or []
            check(43, "clicking your own reaction-chip removes your reaction (chip gone, API empty)", g and rs == [],
                  "chip gone %s, API reactions %s" % (g, short(rs, 120)))
        step(43, r43)

        def r44():
            r = row(page, "thread me " + RUN)
            av = visible(r.locator(T("message-avatar")))
            au = r.locator(T("message-author")).first.inner_text().strip()
            tm = r.locator(T("message-time")).first.inner_text().strip()
            check(44, "each message row shows message-avatar, message-author (the author's name) and a readable message-time",
                  av and u["name"] in au + " " + (dig(call("GET", N0 + "/api/auth/me", None, u["token"])[1], "user.display_name") or "") and au
                  and re.search(r"\d{1,2}:\d{2}", tm) is not None,
                  "avatar %s, author %r, time %r" % (av, au, tm))
        step(44, r44)

        def r45():
            open_channel(page, "quiet")
            page.wait_for_timeout(400)
            txt = page.locator(T("message-list")).inner_text().strip()
            n = page.locator(T("message")).count()
            check(45, "an empty channel shows a non-blank empty-state sentence in message-list and no messages",
                  n == 0 and len(txt) >= 5, "messages %d, text %r" % (n, txt[:80]))
        step(45, r45)
    with_browser(list(STAGES[6]), run)


# ===== E.7 App flows in the browser (rules 46-53) =====
def stage7():
    helper = users("ho")[0]

    def run(page, dialogs):
        page.goto(N0 + "/")
        name = "fl" + RUN
        form = T("auth-form")
        page.fill(form + ' input[name="username"]', name)
        page.fill(form + ' input[name="password"]', PW)
        page.click(T("auth-submit"))
        page.wait_for_function("() => localStorage.getItem('huddle.token') || window.__huddle_token", timeout=3000)
        tok = ui_token(page)
        S = "e7-" + RUN

        def r46():
            es = visible(page.locator(T("empty-state-create-workspace")))
            wf = T("workspace-create-form")
            page.fill(wf + ' input[name="name"]', "Flow Team")
            page.fill(wf + ' input[name="slug"]', S)
            page.click(wf + ' [type="submit"]')
            hv = visible(page.locator(T("workspace-header"), has_text="Flow Team"))
            gv = visible(page.locator('%s[data-channel-name="general"]' % T("channel-entry")))
            check(46, "a new user sees empty-state-create-workspace; workspace-create-form (name, slug) creates it: header shows the name, #general listed",
                  es and hv and gv, "empty state %s, header %s, #general %s" % (es, hv, gv))

        def r47():
            jf = T("join-workspace-form")
            page.fill(jf + ' input[name="code"]', "nosuch" + RUN)
            page.click(T("join-workspace-submit"))
            ev = visible(page.locator(T("join-workspace-error")))
            et = page.locator(T("join-workspace-error")).first.inner_text().strip() if ev else ""
            check(47, "join-workspace-form with a bad code shows the error text in join-workspace-error", ev and et,
                  "visible %s, text %r" % (ev, et))
        step(47, r47)
        step(46, r46)

        def r48():
            page.click(T("new-channel-btn"))
            mv = visible(page.locator("#create-channel-modal"))
            page.fill(T("create-channel-form") + ' input[name="name"]', "planning")
            page.click(T("create-channel-submit"))
            ent = visible(page.locator('%s[data-channel-name="planning"]' % T("channel-entry")))
            tv = visible(page.locator(T("channel-title"), has_text="planning"))
            check(48, "new-channel-btn opens create-channel-modal; creating 'planning' lists it in the sidebar and opens it",
                  mv and ent and tv, "modal %s, sidebar entry %s, channel-title %s" % (mv, ent, tv))
        step(48, r48)

        def r49():
            page.click(T("new-channel-btn"))
            page.fill(T("create-channel-form") + ' input[name="name"]', "planning")
            page.click(T("create-channel-submit"))
            ev = visible(page.locator(T("create-channel-form") + ' [role="alert"]'))
            n = page.locator('%s[data-channel-name="planning"]' % T("channel-entry")).count()
            chans = [c.get("name") for c in ((call("GET", "%s/api/workspaces/%s" % (N0, S), None, tok)[1] or {}).get("channels") or [])]
            check(49, "creating a duplicate channel name shows a visible [role=alert] error in create-channel-form and creates nothing",
                  ev and n == 1 and chans.count("planning") == 1, "alert %s, sidebar entries %d, API channels %s" % (ev, n, chans))
            try:
                page.click(T("create-channel-cancel"))
            except Exception:
                pass
        step(49, r49)

        def r50():
            open_channel(page, "planning")
            page.click(T("channel-settings-btn"))
            mv = visible(page.locator("#channel-settings-modal"))
            page.fill(T("channel-topic-input"), "Roadmap " + RUN)
            page.click(T("channel-topic-submit"))
            page.wait_for_timeout(500)
            page.click(T("channel-settings-close"))
            closed = gone(page.locator("#channel-settings-modal").locator("visible=true")) or not page.locator("#channel-settings-modal").is_visible()
            tv = visible(page.locator(T("channel-topic"), has_text="Roadmap " + RUN))
            topic = [c.get("topic") for c in ((call("GET", "%s/api/workspaces/%s" % (N0, S), None, tok)[1] or {}).get("channels") or []) if c.get("name") == "planning"]
            check(50, "channel-settings-btn opens the modal; channel-topic-input and submit save the topic, shown in channel-topic; close hides it",
                  mv and closed and tv and topic == ["Roadmap " + RUN], "modal %s, closed %s, header topic %s, API topic %s" % (mv, closed, tv, topic))
        step(50, r50)

        def r51():
            page.click(T("workspace-settings-btn"))
            mv = visible(page.locator("#workspace-settings-modal"))
            page.click(T("settings-tab-members"))
            owner = visible(page.locator(T("settings-pane-members")).locator(T("role-badge"), has_text="owner"))
            page.click(T("settings-tab-invitations"))
            page.click(T("create-invitation-btn"))
            page.click(T("create-invitation-submit"))
            cv = visible(page.locator(T("invitation-code")))
            code = page.locator(T("invitation-code")).first.inner_text().strip() if cv else ""
            acc = call("POST", "%s/api/invitations/%s/accept" % (N0, urllib.parse.quote(code, safe="")), {}, helper["token"])[0] if code else None
            page.click(T("workspace-settings-close"))
            check(51, "workspace settings: the members tab shows your owner role-badge; the invitations tab creates a working invitation-code",
                  mv and owner and cv and acc == 200, "modal %s, owner badge %s, code %r, accept with it -> %s" % (mv, owner, code, acc))
        step(51, r51)

        def r52():
            page.click(T("logout-btn"))
            av = visible(page.locator(T("auth-form")))
            cleared = not ui_token(page)
            if page.get_attribute(T("auth-form"), "data-mode") != "login":
                page.click(T("auth-toggle"))
            page.fill(form + ' input[name="username"]', name)
            page.fill(form + ' input[name="password"]', PW)
            page.click(T("auth-submit"))
            hv = visible(page.locator(T("workspace-header"), has_text="Flow Team"), 4000)
            pv = visible(page.locator('%s[data-channel-name="planning"]' % T("channel-entry")))
            check(52, "logout-btn shows auth-form and clears the token; logging in (auth-toggle to login mode) restores the same workspace",
                  av and cleared and hv and pv, "auth-form %s, token cleared %s, header %s, #planning %s" % (av, cleared, hv, pv))
        step(52, r52)

        def r53():
            sb = page.locator(T("channel-list")).bounding_box()
            mb = page.locator(T("message-list")).bounding_box()
            bg = page.evaluate("""() => { let el = document.querySelector('[data-testid="channel-list"]');
                while (el) { const c = getComputedStyle(el).backgroundColor;
                  if (c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent') return c; el = el.parentElement; }
                return ''; }""")
            hdr = visible(page.locator(T("channel-title"))) and visible(page.locator(T("workspace-header")))
            check(53, "three-pane layout at 1280x900: a coloured sidebar left of the message list, with workspace-header and channel-title visible",
                  sb and mb and sb["x"] + sb["width"] <= mb["x"] + 5 and bg not in ("", "rgb(255, 255, 255)") and hdr,
                  "sidebar box %s, messages box %s, sidebar colour %r, headers %s" % (sb, mb, bg, hdr))
        step(53, r53)
    with_browser(list(STAGES[7]), run)


def main():
    wd = threading.Timer(26, _time_is_up)
    wd.daemon = True
    wd.start()
    boot()
    for n, fn in ((1, stage1), (2, stage2), (3, stage3), (4, stage4), (5, stage5), (6, stage6), (7, stage7)):
        if n not in SELECTED:
            continue
        try:
            fn()
        except Exception as e:
            msg = str(e).split("\n")[0][:160]
            for r in STAGES[n]:
                bad(r, "not checked: %s" % msg if isinstance(e, RuntimeError) else "check stopped (%s: %s)" % (type(e).__name__, msg))
        for r in STAGES[n]:
            bad(r, "no result")  # only counts rules that were never decided
    with _lock:
        print("RESULT: %d passed, %d failed" % (passed, failed), flush=True)
    os._exit(0 if failed == 0 else 1)


main()
