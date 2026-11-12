#!/usr/bin/env python3
"""Checks for component C (features built on messages). Prints PASS/FAIL per rule.

CHECK_STAGE=1..5 runs one stage (one spec file); unset or "all" runs every stage.
Each stage sets up its own users, workspace and channels, so stages run independently.
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
PW = "correct-horse-1"
RUN = "%x%x" % (int(time.time()), random.randrange(4096))
T0 = time.time()
# AIDEV-NOTE: the agent's commands are cut off after 30s. Requests stop at DEADLINE and a watchdog
# prints every unreached rule as FAIL plus the RESULT line at 22s, so the summary is never lost.
DEADLINE = 21

STAGES = {1: range(1, 14), 2: range(14, 24), 3: range(24, 32), 4: range(32, 43), 5: range(43, 57)}
_sel = os.environ.get("CHECK_STAGE", "all")
SELECTED = sorted(STAGES) if _sel == "all" else [int(x) for x in _sel.split(",")]


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
            say("FAIL %s not reached: the check's 22 second time budget ran out (earlier rules were slow)" % r)
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


def bad(rule, msg):
    global failed
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


# ---- HTTP ----
def request(method, url, data=None, token=None, headers=None, timeout=5):
    """Returns (status, raw-bytes, headers). Status 0 means no answer."""
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
    """JSON request. Returns (status, parsed-json-or-None)."""
    data = None if body is None else json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json"} if data is not None else {}
    st, raw, _ = request(method, url, data, token, hdrs, timeout)
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


def sids(xs):
    return sorted(str(x) for x in (xs or []) if x is not None) if isinstance(xs, list) else None


def short(d, n=200):
    return json.dumps(d)[:n]


ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|\+00:00)$")


def healthy():
    for u in (N0, N1, N2):
        if call("GET", u + "/api/health", timeout=2)[0] != 200:
            return False
    return True


def boot():
    if not healthy():
        try:
            log = open("/tmp/start.log", "w")
            subprocess.Popen(["/app/start.sh"], cwd="/app", stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True)
        except OSError as e:
            say("FAIL 0 could not start /app/start.sh: %s" % e)
        for _ in range(20):
            if healthy():
                break
            time.sleep(1)
    if not healthy():
        say("FAIL 0 cluster is not answering /api/health on 8000, 8001, 8002 (see /tmp/start.log)")
        print("RESULT: 0 passed, 1 failed")
        sys.exit(1)


# ---- basics (from Specs A and B, or the basics the README asks for) ----
def reg(label):
    name = "%s%s" % (label, RUN)
    st, d = call("POST", N0 + "/api/auth/register", {"username": name, "password": PW})
    return {"name": name, "token": dig(d, "token"), "id": dig(d, "user.id")}


def mkws(slug, u):
    st, d = call("POST", N0 + "/api/workspaces", {"slug": slug, "name": "Feat " + slug}, u["token"])
    return dig(d, "general_channel.id")


def newchan(slug, name, u, private=False):
    st, d = call("POST", "%s/api/workspaces/%s/channels" % (N0, slug), {"name": name, "is_private": private}, u["token"])
    return dig(d, "channel.id")


def join(cid, u, node=N0):
    return call("POST", "%s/api/channels/%s/join" % (node, cid), {}, u["token"])[0]


def post(cid, u, body, node=N0, **extra):
    payload = {"body": body}
    payload.update(extra)
    return call("POST", "%s/api/channels/%s/messages" % (node, cid), payload, u["token"])


def messages(cid, u, node=N0):
    st, d = call("GET", "%s/api/channels/%s/messages" % (node, cid), None, u["token"])
    return st, (d or {}).get("messages") if isinstance(d, dict) else None


def find_msg(items, mid):
    for m in items or []:
        if isinstance(m, dict) and sid(m.get("id")) == sid(mid):
            return m
    return None


def member_names(cid, u, node=N0):
    st, d = call("GET", "%s/api/channels/%s/members" % (node, cid), None, u["token"])
    return st, sorted(str(m.get("username")) for m in ((d or {}).get("members") or []) if isinstance(m, dict))


def mentions_of(d):
    return sids(dig(d, "message.mentions"))


def mention_count(cid, u):
    return dig(call("GET", "%s/api/channels/%s/read" % (N0, cid), None, u["token"])[1], "read_state.mention_count")


def users(*labels):
    out = [reg(l) for l in labels]
    if not all(u["token"] for u in out):
        raise RuntimeError("setup: could not register users (register must work first)")
    return out


# ---- WebSocket ----
async def ws_open(port, token):
    return await websockets.connect("ws://127.0.0.1:%d/api/ws?token=%s" % (port, token), open_timeout=3)


async def wait_for(ws, pred, timeout, seen=None):
    end = time.time() + timeout
    while True:
        left = end - time.time()
        if left <= 0:
            return None
        try:
            f = json.loads(await asyncio.wait_for(ws.recv(), left))
        except Exception:
            return None
        if seen is not None:
            seen.append(f)
        if isinstance(f, dict) and pred(f):
            return f


async def subscribe(ws, cid, timeout=3):
    await ws.send(json.dumps({"type": "subscribe", "channel_id": cid}))
    f = await wait_for(ws, lambda f: f.get("type") == "subscribed" and sid(f.get("channel_id")) == sid(cid), timeout)
    return (f or {}).get("head_event_id")


async def durable(ws, cid, n, timeout, after=None):
    """Collects up to n durable frames (frames with an event_id) of this channel.

    Frames with event_id <= `after` (the head given at subscribe) are older events still in flight
    when the socket subscribed; they are skipped, not counted."""
    got, end = [], time.time() + timeout
    while len(got) < n and time.time() < end:
        f = await wait_for(ws, lambda f: sid(f.get("channel_id")) == sid(cid) and "event_id" in f
                           and not (isinstance(after, int) and isinstance(f.get("event_id"), int) and f["event_id"] <= after),
                           end - time.time())
        if f is None:
            break
        got.append(f)
    return got


def ws_rule(rules, coro_fn, limit=6):
    """Runs one async WebSocket rule; on error or timeout fails every rule in `rules` not yet decided."""
    if websockets is None:
        for r in rules:
            bad(r, "the python `websockets` package is not available to the check")
        return
    try:
        asyncio.run(asyncio.wait_for(coro_fn(), max(1, min(limit, DEADLINE + 1 - (time.time() - T0)))))
    except Exception as e:
        for r in rules:
            bad(r, "error or timeout over WebSocket (%s %s)" % (type(e).__name__, e))


# ---- multipart upload ----
def upload(u, filename, ctype, data, node=N0, field="file", token=True):
    b = "----chk" + RUN
    head = ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\nContent-Type: %s\r\n\r\n'
            % (b, field, filename, ctype)).encode()
    body = head + data + ("\r\n--%s--\r\n" % b).encode()
    st, raw, _ = request("POST", node + "/api/files", body, u["token"] if token else None,
                         {"Content-Type": "multipart/form-data; boundary=" + b}, timeout=8)
    try:
        return st, json.loads(raw) if raw else None
    except Exception:
        return st, None


UF = ["id", "username", "display_name", "timezone", "avatar_url", "status_text", "status_emoji"]


# ===== C.1 Profiles and DMs (rules 1-13) =====
def stage1():
    o, a, b, c = users("po", "pa", "pb", "pc")
    S = "f1-" + RUN
    gen = mkws(S, o)
    for u in (a, b, c):
        join(gen, u)

    st, d = call("GET", "%s/api/users/%s" % (N0, a["id"]), None, o["token"])
    u = dig(d, "user") or {}
    check(1, "GET /api/users/{id} returns 200 and a full user", st == 200 and all(k in u for k in UF)
          and sid(u.get("id")) == sid(a["id"]) and u.get("username") == a["name"],
          "status %s, user %s" % (st, short(u)))
    s1 = call("GET", "%s/api/users/99999999" % N0, None, o["token"])[0]
    s2 = call("GET", "%s/api/users/%s" % (N0, a["id"]))[0]
    check(2, "unknown user id 404, no token 401", (s1, s2) == (404, 401), "got %s and %s" % (s1, s2))

    me = "%s/api/users/me" % N0
    s1, _ = call("PATCH", me, {"status_text": "in a meeting", "status_emoji": ":calendar:"}, a["token"])
    s2, d2 = call("PATCH", me, {"display_name": "Alice Cee"}, a["token"])
    u2 = dig(d2, "user") or {}
    check(3, "PATCH /api/users/me changes only the fields sent and returns {user}",
          s1 == 200 and s2 == 200 and u2.get("display_name") == "Alice Cee" and u2.get("status_text") == "in a meeting"
          and u2.get("status_emoji") == ":calendar:" and u2.get("username") == a["name"],
          "statuses %s/%s, user %s" % (s1, s2, short(u2)))
    g = dig(call("GET", "%s/api/users/%s" % (N2, a["id"]), None, b["token"])[1], "user") or {}
    m = dig(call("GET", "%s/api/auth/me" % N1, None, a["token"])[1], "user") or {}
    check(4, "profile changes are visible on 8002 (users/{id}) and 8001 (auth/me)",
          g.get("display_name") == "Alice Cee" and g.get("status_text") == "in a meeting" and m.get("display_name") == "Alice Cee",
          "8002 %s, 8001 %s" % (short(g, 150), short(m, 150)))
    s1, d1 = call("PATCH", me, {"timezone": "America/New_York"}, a["token"])
    s2, _ = call("PATCH", me, {"timezone": "Mars/Phobos"}, a["token"])
    tz = dig(call("GET", "%s/api/users/%s" % (N0, a["id"]), None, a["token"])[1], "user.timezone")
    check(5, "a valid IANA timezone is saved, an invalid one is 400 and changes nothing",
          s1 == 200 and dig(d1, "user.timezone") == "America/New_York" and s2 == 400 and tz == "America/New_York",
          "valid -> %s, invalid -> %s, stored %r" % (s1, s2, tz))
    got = [call("PATCH", me, body, a["token"])[0] for body in (
        {"display_name": "d" * 80}, {"display_name": "d" * 81}, {"status_text": "s" * 100},
        {"status_text": "s" * 101}, {"status_emoji": "e" * 65}, {"display_name": ""},
        {"avatar_url": "https://x.example/" + "a" * 2030}, {"avatar_url": "https://x.example/" + "a" * 2031})]
    dn = dig(call("GET", "%s/api/users/%s" % (N0, a["id"]), None, a["token"])[1], "user.display_name")
    check(6, "length limits: display_name 1-80, status_text 0-100, status_emoji 0-64, avatar_url 0-2048 (else 400, nothing saved)",
          got == [200, 400, 200, 400, 400, 400, 200, 400] and dn == "d" * 80,
          "statuses %s (expected [200, 400, 200, 400, 400, 400, 200, 400]), stored display_name length %s" % (got, len(dn or "")))
    s1 = call("PATCH", me, {"display_name": "x"})[0]
    s2 = call("PATCH", me, {"display_name": 123}, a["token"])[0]
    check(7, "PATCH /api/users/me without a token 401, a non-string field 400", (s1, s2) == (401, 400),
          "got %s and %s" % (s1, s2))

    async def r8():
        ws = await ws_open(8002, b["token"])
        try:
            await asyncio.sleep(0.2)
            call("PATCH", me, {"display_name": "Alice WS"}, a["token"])
            # earlier PATCHes (rule 6) may still be in flight: wait for the frame of this one
            f = await wait_for(ws, lambda f: f.get("type") == "user.updated"
                               and (f.get("user") or {}).get("display_name") == "Alice WS", 3)
            uu = (f or {}).get("user") or {}
            check(8, "PATCH on 8000 sends user.updated with the new user to a socket on 8002, with no channel_id or event_id",
                  f is not None and sid(uu.get("id")) == sid(a["id"]) and uu.get("display_name") == "Alice WS"
                  and "channel_id" not in f and "event_id" not in f, "frame %s" % short(f))
        finally:
            await ws.close()
    ws_rule([8], r8)

    s1, d1 = call("POST", N0 + "/api/dms", {"user_id": b["id"]}, a["token"])
    s2, d2 = call("POST", N0 + "/api/dms", {"user_id": b["id"]}, a["token"])
    s3, d3 = call("POST", N1 + "/api/dms", {"user_id": a["id"]}, b["token"])
    DM = dig(d1, "channel_id")
    check(9, "POST /api/dms returns 200 {channel_id}, the same id on repeat and in the other direction",
          (s1, s2, s3) == (200, 200, 200) and DM is not None and sid(DM) == sid(dig(d2, "channel_id")) == sid(dig(d3, "channel_id")),
          "statuses %s/%s/%s, ids %s/%s/%s" % (s1, s2, s3, DM, dig(d2, "channel_id"), dig(d3, "channel_id")))
    p1 = post(DM, a, "dm hi")[0]
    p2 = post(DM, b, "dm back", node=N2)[0]
    sl, lst = messages(DM, a, node=N1)
    sm, names = member_names(DM, a)
    check(10, "both DM users can post and read (any node), and the DM has exactly those two members",
          (p1, p2, sl) == (201, 201, 200) and [x.get("body") for x in lst or []][:2] == ["dm back", "dm hi"]
          and names == sorted([a["name"], b["name"]]),
          "posts %s/%s, list %s %s, members %s %s" % (p1, p2, sl, [x.get("body") for x in lst or []][:2], sm, names))
    s1 = messages(DM, c)[0]
    s2 = post(DM, c, "intruder")[0]
    check(11, "a third user gets 403 reading or posting in the DM", (s1, s2) == (403, 403), "got %s and %s" % (s1, s2))
    s1 = call("POST", N0 + "/api/dms", {"user_id": 99999999}, a["token"])[0]
    s2 = call("POST", N0 + "/api/dms", {}, a["token"])[0]
    s3 = call("POST", N0 + "/api/dms", {"user_id": b["id"]})[0]
    check(12, "DM with an unknown user 404, without user_id 400, without a token 401", (s1, s2, s3) == (404, 400, 401),
          "got %s, %s, %s" % (s1, s2, s3))

    async def r13():
        ws = await ws_open(8001, b["token"])
        try:
            head = await subscribe(ws, DM)
            if not isinstance(head, int):
                bad(13, "subscribing to the DM channel gave no head_event_id")
                return
            post(DM, a, "dm live")
            got = await durable(ws, DM, 1, 3, after=head)
            f = got[0] if got else None
            check(13, "a DM message reaches the other user's socket as message.created with the next event_id",
                  f is not None and f.get("type") == "message.created" and f.get("event_id") == head + 1,
                  "head %s, frame %s" % (head, short(f)))
        finally:
            await ws.close()
    ws_rule([13], r13)


# ===== C.2 Reactions (rules 14-23) =====
def stage2():
    o, a, b = users("ro", "ra", "rb")
    S = "f2-" + RUN
    gen = mkws(S, o)
    for u in (a, b):
        join(gen, u)
    R = newchan(S, "react", o)
    for u in (a, b):
        join(R, u)
    mid = dig(post(R, o, "react to me")[1], "message.id")
    E = "\U0001F44D"
    enc = urllib.parse.quote(E, safe="")
    base = "%s/api/messages/%s/reactions" % (N0, mid)

    def entry(reacts, emoji):
        for x in reacts or []:
            if isinstance(x, dict) and x.get("emoji") == emoji:
                return x
        return None

    s, d = call("POST", base, {"emoji": E}, a["token"])
    rs = (d or {}).get("reactions")
    e = entry(rs, E) or {}
    check(14, "first reaction returns 200 {reactions} with {emoji, count 1, user_ids}",
          s == 200 and isinstance(rs, list) and len(rs) == 1 and e.get("count") == 1 and sids(e.get("user_ids")) == [str(a["id"])],
          "status %s, reactions %s" % (s, short(rs)))
    s, d = call("POST", base, {"emoji": E}, a["token"])
    e = entry((d or {}).get("reactions"), E) or {}
    check(15, "the same user reacting again is a no-op (200, count stays 1)", s == 200 and e.get("count") == 1,
          "status %s, entry %s" % (s, short(e)))
    s, d = call("POST", base.replace(N0, N1), {"emoji": E}, b["token"])
    e = entry((d or {}).get("reactions"), E) or {}
    check(16, "a second user (on 8001) makes count 2 with both user_ids",
          s == 200 and e.get("count") == 2 and sids(e.get("user_ids")) == sorted([str(a["id"]), str(b["id"])]),
          "status %s, entry %s" % (s, short(e)))
    sl, lst = messages(R, o, node=N2)
    e = entry((find_msg(lst, mid) or {}).get("reactions"), E) or {}
    check(17, "the message listing (on 8002) shows the reaction with count 2", sl == 200 and e.get("count") == 2,
          "status %s, entry %s" % (sl, short(e)))
    s1, d1 = call("DELETE", "%s/%s" % (base, enc), None, a["token"])
    s2, d2 = call("DELETE", "%s/%s" % (base, enc), None, a["token"])
    e1, e2 = entry((d1 or {}).get("reactions"), E) or {}, entry((d2 or {}).get("reactions"), E) or {}
    check(18, "DELETE removes only the caller's reaction (200 {reactions}, count 1), and again is a no-op",
          s1 == 200 and e1.get("count") == 1 and sids(e1.get("user_ids")) == [str(b["id"])] and s2 == 200 and e2.get("count") == 1,
          "statuses %s/%s, entries %s / %s" % (s1, s2, short(e1, 120), short(e2, 120)))
    s, d = call("DELETE", "%s/%s" % (base, enc), None, b["token"])
    check(19, "when the last user removes it, the emoji leaves the list", s == 200 and (d or {}).get("reactions") == [],
          "status %s, reactions %s" % (s, short((d or {}).get("reactions"))))
    call("POST", base, {"emoji": "\U0001F389"}, a["token"])
    s, d = call("POST", base, {"emoji": "eyes"}, a["token"])
    rs = (d or {}).get("reactions") or []
    check(20, "different emojis are separate entries", s == 200 and sorted(str(x.get("emoji")) for x in rs) == sorted(["\U0001F389", "eyes"])
          and all(x.get("count") == 1 for x in rs), "status %s, reactions %s" % (s, short(rs)))
    got = [call("POST", "%s/api/messages/99999999/reactions" % N0, {"emoji": E}, a["token"])[0],
           call("DELETE", "%s/api/messages/99999999/reactions/%s" % (N0, enc), None, a["token"])[0],
           call("POST", base, {}, a["token"])[0], call("POST", base, {"emoji": ""}, a["token"])[0],
           call("POST", base, {"emoji": E})[0]]
    check(21, "unknown message 404 (POST and DELETE), missing or empty emoji 400, no token 401",
          got == [404, 404, 400, 400, 401], "got %s" % got)

    async def r22():
        m2 = dig(post(R, o, "live reactions")[1], "message.id")
        ws = await ws_open(8001, o["token"])
        try:
            head = await subscribe(ws, R)
            if not isinstance(head, int):
                bad(22, "subscribe gave no head_event_id")
                return
            b2 = "%s/api/messages/%s/reactions" % (N0, m2)
            call("POST", b2, {"emoji": "fire"}, a["token"])
            call("POST", b2, {"emoji": "fire"}, a["token"])  # duplicate: must not emit an event
            call("DELETE", b2 + "/fire", None, a["token"])
            got = await durable(ws, R, 3, 2.5, after=head)
            kinds, evs = [f.get("type") for f in got], [f.get("event_id") for f in got]
            check(22, "add, duplicate add, remove give exactly reaction.added then reaction.removed with the next event_ids",
                  kinds == ["reaction.added", "reaction.removed"] and evs == [head + 1, head + 2],
                  "head %s, kinds %s, ids %s" % (head, kinds, evs))
        finally:
            await ws.close()
    ws_rule([22], r22)

    P = newchan(S, "priv", o, private=True)
    pm = dig(post(P, o, "secret")[1], "message.id")
    s = call("POST", "%s/api/messages/%s/reactions" % (N0, pm), {"emoji": E}, a["token"])[0]
    check(23, "reacting to a message in a private channel you are not in is 403", s == 403, "got %s" % s)


# ===== C.3 Files (rules 24-31) =====
def stage3():
    o, a, b = users("fo", "fa", "fb")
    S = "f3-" + RUN
    gen = mkws(S, o)
    for u in (a, b):
        join(gen, u)
    data = ("hello file " + RUN).encode()
    st, d = upload(a, "hello.txt", "text/plain", data)
    f = dig(d, "file") or {}
    FID = f.get("id")
    check(24, "upload returns 201 {file} with id, uploader_id, filename, content_type, size, created_at",
          st == 201 and FID is not None and sid(f.get("uploader_id")) == sid(a["id"]) and f.get("filename") == "hello.txt"
          and str(f.get("content_type", "")).startswith("text/plain") and f.get("size") == len(data) and ISO.match(str(f.get("created_at"))),
          "status %s, file %s" % (st, short(f)))
    s1, d1 = call("GET", "%s/api/files/%s" % (N2, FID), None, b["token"])
    s2 = call("GET", "%s/api/files/99999999" % N0, None, a["token"])[0]
    s3 = upload(a, "x.txt", "text/plain", b"x", token=False)[0]
    g = dig(d1, "file") or {}
    check(25, "GET /api/files/{id} on 8002 returns the same file; unknown id 404; upload without a token 401",
          s1 == 200 and sid(g.get("id")) == sid(FID) and g.get("size") == len(data) and (s2, s3) == (404, 401),
          "got %s %s, %s, %s" % (s1, short(g, 120), s2, s3))
    st, raw, hd = request("GET", "%s/api/files/%s/download" % (N1, FID), None, b["token"])
    ct, cd = (hd.get("Content-Type") or "") if hd else "", (hd.get("Content-Disposition") or "") if hd else ""
    check(26, "download on 8001 returns the exact bytes, the Content-Type, and the filename in Content-Disposition",
          st == 200 and raw == data and ct.startswith("text/plain") and "hello.txt" in cd,
          "status %s, %d bytes (want %d), Content-Type %r, Content-Disposition %r" % (st, len(raw), len(data), ct, cd))
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8 + b"\x00\x00end"
    st, d = upload(a, "pic.png", "image/png", png, node=N1)
    pid = dig(d, "file.id")
    s2, raw, hd = request("GET", "%s/api/files/%s/download" % (N0, pid), None, a["token"])
    ct = (hd.get("Content-Type") or "") if hd else ""
    check(27, "binary bytes (a PNG with zero bytes) round-trip exactly as image/png",
          st == 201 and s2 == 200 and raw == png and ct.startswith("image/png"),
          "upload %s, download %s, %d of %d bytes match, Content-Type %r" % (st, s2, len(raw) if raw == png else 0, len(png), ct))
    MiB = 10 * 1024 * 1024
    s1 = upload(a, "max.bin", "application/octet-stream", b"\x01" * MiB)[0]
    s2 = upload(a, "big.bin", "application/octet-stream", b"\x01" * (MiB + 1))[0]
    check(28, "a file of exactly 10 MiB is accepted (201), one byte more is 413", (s1, s2) == (201, 413),
          "10 MiB -> %s, 10 MiB + 1 -> %s%s" % (s1, s2, " (no answer: read the body, then answer 413)" if s2 == 0 else ""))
    s = upload(a, "nofield.txt", "text/plain", b"abc", field="other")[0]
    check(29, "a multipart body without a `file` part is 400", s == 400, "got %s" % s)
    F = newchan(S, "files", o)
    join(F, a)
    join(F, b)
    st, d = post(F, a, "see attached", file_ids=[FID])
    fl = dig(d, "message.files")
    mid = dig(d, "message.id")
    _, lst = messages(F, b, node=N2)
    lf = (find_msg(lst, mid) or {}).get("files")
    check(30, "posting with file_ids attaches the file: message.files holds the FileObj, also in the listing",
          st == 201 and isinstance(fl, list) and len(fl) == 1 and sid(fl[0].get("id")) == sid(FID) and fl[0].get("filename") == "hello.txt"
          and isinstance(lf, list) and [sid(x.get("id")) for x in lf] == [sid(FID)],
          "status %s, files %s, listed %s" % (st, short(fl, 150), short(lf, 100)))
    bf = dig(upload(b, "bobs.txt", "text/plain", b"bob")[1], "file.id")
    got = [post(F, a, "again", file_ids=[FID])[0], post(F, a, "not mine", file_ids=[bf])[0],
           post(F, a, "unknown", file_ids=[99999999])[0]]
    check(31, "file_ids with an already attached file, another user's file, or an unknown id is 400",
          got == [400, 400, 400], "got %s" % got)


# ===== C.4 Slash commands (rules 32-42) =====
SHRUG = "\u00af\\_(\u30c4)_/\u00af"


def stage4():
    o, a, b, e = users("so", "sa", "sb", "se")
    S = "f4-" + RUN
    gen = mkws(S, o)
    for u in (a, b, e):
        join(gen, u)
    C = newchan(S, "slash", o)
    for u in (a, b):
        join(C, u)
    st, d = post(C, a, "/me waves")
    check(32, "/me waves posts a normal message with body kept as \"/me waves\"",
          st == 201 and dig(d, "message.body") == "/me waves", "status %s, body %r" % (st, dig(d, "message.body")))
    got = [post(C, a, "/me")[0], post(C, a, "/me   ")[0]]
    check(33, "/me with no text is 400", got == [400, 400], "got %s" % got)
    s1, d1 = post(C, a, "/shrug oh well")
    s2, d2 = post(C, a, "/shrug")
    check(34, "/shrug appends the shrug emoticon after a space; alone it is just the emoticon",
          s1 == 201 and dig(d1, "message.body") == "oh well " + SHRUG and s2 == 201 and dig(d2, "message.body") == SHRUG,
          "got %s %r and %s %r" % (s1, dig(d1, "message.body"), s2, dig(d2, "message.body")))
    s, d = post(C, o, "/topic Sprint planning")
    s2 = post(C, o, "/topic " + "t" * 251)[0]
    check(35, "/topic sets the topic: 201 with message null and the updated channel; over 250 chars 400",
          s == 201 and isinstance(d, dict) and "message" in d and d["message"] is None and dig(d, "channel.topic") == "Sprint planning" and s2 == 400,
          "status %s, body %s, too long -> %s" % (s, short(d), s2))
    s, d = post(C, o, "/invite @%s" % e["name"])
    _, names = member_names(C, o)
    check(36, "/invite @user adds a workspace member to the channel: 201, message null, invited_user",
          s == 201 and isinstance(d, dict) and d.get("message") is None and "message" in d
          and sid(dig(d, "invited_user.id")) == sid(e["id"]) and e["name"] in names,
          "status %s, body %s, members %s" % (s, short(d), names))
    s = post(C, o, "/invite @nosuch%s" % RUN)[0]
    check(37, "/invite of an unknown username is 404", s == 404, "got %s" % s)
    s1 = post(C, a, "/archive")[0]
    s2, d2 = post(C, o, "/archive")
    p1 = post(C, o, "while archived")[0]
    s3, d3 = post(C, o, "/unarchive")
    p2 = post(C, o, "after unarchive")[0]
    check(38, "/archive by a plain member is 403", s1 == 403, "got %s" % s1)
    check(39, "/archive and /unarchive by the owner: 201, message null, channel.is_archived true then false; posts 423 while archived",
          s2 == 201 and dig(d2, "channel.is_archived") is True and (d2 or {}).get("message", "x") is None and p1 == 423
          and s3 == 201 and dig(d3, "channel.is_archived") is False and p2 == 201,
          "archive %s %s, post %s, unarchive %s %s, post %s" % (s2, dig(d2, "channel.is_archived"), p1, s3, dig(d3, "channel.is_archived"), p2))
    got = [post(C, a, "/foo bar")[0], post(C, a, "/")[0]]
    check(40, "an unknown command (/foo, or a bare /) is 400", got == [400, 400], "got %s" % got)
    s, d = post(C, a, "//literal text")
    check(41, "//literal text is posted as a normal message with the body unchanged",
          s == 201 and dig(d, "message.body") == "//literal text", "status %s, body %r" % (s, dig(d, "message.body")))

    async def r42():
        ws = await ws_open(8002, a["token"])
        try:
            head = await subscribe(ws, C)
            if not isinstance(head, int):
                bad(42, "subscribe gave no head_event_id")
                return
            post(C, o, "/topic Live topic")
            got = await durable(ws, C, 2, 1.5, after=head)
            kinds = [f.get("type") for f in got]
            _, lst = messages(C, o)
            stored = [m.get("body") for m in lst or [] if str(m.get("body", "")).startswith(("/topic", "/invite", "/archive", "/unarchive"))]
            check(42, "side-effect commands store no message: /topic gives only channel.updated, and none appear in the listing",
                  kinds == ["channel.updated"] and got[0].get("event_id") == head + 1 and stored == [],
                  "head %s, frames %s, stored command messages %s" % (head, [(f.get("type"), f.get("event_id")) for f in got], stored))
        finally:
            await ws.close()
    ws_rule([42], r42)


# ===== C.5 User groups, @group-handle, @here (rules 43-56) =====
def stage5():
    o, a, b, c, d_, e, y = users("go", "ga", "gb", "gc", "gd", "ge", "gy")
    S = "f5-" + RUN
    gen = mkws(S, o)
    for u in (a, b, c, d_, e):
        join(gen, u)
    G = "%s/api/workspaces/%s/groups" % (N0, S)
    H = "eng-" + RUN
    s, d = call("POST", G, {"handle": H, "name": "Engineers", "user_ids": [b["id"], c["id"], d_["id"]]}, a["token"])
    g = dig(d, "group") or {}
    check(43, "a member creates a group: 201 {group} with id, handle, name, member_user_ids (no `members`)",
          s == 201 and g.get("id") is not None and g.get("handle") == H and g.get("name") == "Engineers" and "members" not in g
          and sids(g.get("member_user_ids")) == sorted(str(u["id"]) for u in (b, c, d_)),
          "status %s, group %s" % (s, short(g)))
    s, d = call("GET", G.replace(N0, N1), None, b["token"])
    lst = [x for x in ((d or {}).get("groups") or []) if isinstance(x, dict) and x.get("handle") == H]
    check(44, "GET groups (on 8001) returns 200 {groups} including the new group with its members",
          s == 200 and len(lst) == 1 and sids(lst[0].get("member_user_ids")) == sorted(str(u["id"]) for u in (b, c, d_)),
          "status %s, matching %s" % (s, short(lst)))
    got = [call("POST", G, {"handle": H, "name": "Dup", "user_ids": []}, b["token"])[0]] + \
          [call("POST", G, {"handle": h, "name": "Bad", "user_ids": []}, b["token"])[0] for h in ("Has Space", "UPPER", "", "x")]
    check(45, "duplicate handle 409; handle with a space, uppercase, empty or 1 char is 400", got == [409, 400, 400, 400, 400],
          "got %s" % got)
    got = [call("POST", G, {"handle": "out-" + RUN, "name": "Out", "user_ids": []}, y["token"])[0],
           call("GET", G, None, y["token"])[0], call("GET", G)[0]]
    check(46, "a non-member of the workspace gets 403 on POST and GET; no token 401", got == [403, 403, 401], "got %s" % got)
    GH = "%s/%s" % (G, H)
    s, d = call("PATCH", GH, {"name": "Eng", "user_ids": [b["id"], c["id"]]}, a["token"])
    check(47, "the creator PATCHes name and user_ids: 200 {group} with the new member_user_ids",
          s == 200 and dig(d, "group.name") == "Eng" and sids(dig(d, "group.member_user_ids")) == sorted([str(b["id"]), str(c["id"])]),
          "status %s, group %s" % (s, short(dig(d, "group"))))
    s1 = call("PATCH", GH, {"name": "Nope"}, b["token"])[0]
    s2, d2 = call("PATCH", GH, {"name": "Eng Team"}, o["token"])
    s3 = call("PATCH", "%s/nosuch-%s" % (G, RUN), {"name": "x"}, a["token"])[0]
    check(48, "PATCH by another member 403, by the workspace owner 200, unknown handle 404",
          (s1, s2, s3) == (403, 200, 404) and dig(d2, "group.name") == "Eng Team", "got %s, %s, %s" % (s1, s2, s3))
    s1 = call("DELETE", GH, None, b["token"])[0]
    s2, d2 = call("DELETE", GH, None, a["token"])
    s3, d3 = call("DELETE", GH, None, a["token"])
    left = [x.get("handle") for x in ((call("GET", G, None, a["token"])[1] or {}).get("groups") or []) if isinstance(x, dict)]
    check(49, "DELETE by another member 403; by the creator 200 {deleted: true}, again 200 {deleted: true}; then gone from the list",
          s1 == 403 and s2 == 200 and d2 == {"deleted": True} and s3 == 200 and d3 == {"deleted": True} and H not in left,
          "got %s, %s %s, %s %s, still listed %s" % (s1, s2, d2, s3, d3, H in left))

    T = "team-" + RUN
    call("POST", G, {"handle": T, "name": "Team", "user_ids": [a["id"], b["id"], c["id"], d_["id"]]}, a["token"])
    M = newchan(S, "ment", o)
    for u in (a, b, c):
        join(M, u)  # d_ is in the group but not in the channel; o is in the channel but not in the group
    s, d = post(M, a, "@%s standup" % T)
    check(50, "@group-handle mentions group members who are in the channel, minus the author",
          s == 201 and mentions_of(d) == sorted([str(b["id"]), str(c["id"])]), "status %s, mentions %s" % (s, mentions_of(d)))
    s, d = post(M, a, "@%s and @%s and @%s" % (T, b["name"], a["name"]))
    s2, d2 = post(M, a, "@channel and @%s and @%s" % (T, b["name"]))
    check(51, "mentions from @group, @username and @channel are deduplicated and never include the author",
          s == 201 and mentions_of(d) == sorted([str(b["id"]), str(c["id"])])
          and s2 == 201 and mentions_of(d2) == sorted([str(o["id"]), str(b["id"]), str(c["id"])]),
          "group+user: %s %s; @channel+group+user: %s %s (want every channel member but the author, once)"
          % (s, mentions_of(d), s2, mentions_of(d2)))
    mb0, ma0 = mention_count(M, b), mention_count(M, a)
    post(M, a, "@%s ping @%s" % (T, a["name"]))
    mb1, ma1 = mention_count(M, b), mention_count(M, a)
    check(52, "a group mention raises a member's mention_count by 1 and never the author's",
          isinstance(mb0, int) and mb1 == mb0 + 1 and ma1 == ma0, "member %s -> %s, author %s -> %s" % (mb0, mb1, ma0, ma1))

    async def r53_54():
        wb = await ws_open(8001, b["token"])  # in the channel, socket open
        wa = await ws_open(8000, a["token"])  # the author, socket open
        we = await ws_open(8002, e["token"])  # socket open but not in the channel
        try:
            await asyncio.sleep(0.5)
            s, d = post(M, a, "@here lunch?")
            check(53, "@here mentions channel members with an open WebSocket (any node), not the author, not non-members",
                  s == 201 and mentions_of(d) == [str(b["id"])], "status %s, mentions %s (expected only %s)" % (s, mentions_of(d), b["id"]))
        finally:
            await wb.close()
        await asyncio.sleep(1.0)
        try:
            s, d = post(M, a, "@here still there?")
            check(54, "after that member closes their socket, @here no longer mentions them",
                  s == 201 and mentions_of(d) == [], "status %s, mentions %s" % (s, mentions_of(d)))
        finally:
            await wa.close()
            await we.close()
    ws_rule([53, 54], r53_54, limit=7)

    s, d = post(M, a, "@nosuch%s hello" % RUN)
    check(55, "an @handle that is neither a user nor a group is a normal message with no mentions",
          s == 201 and mentions_of(d) == [], "status %s, mentions %s" % (s, mentions_of(d)))
    call("DELETE", "%s/%s" % (G, T), None, a["token"])
    s, d = post(M, a, "@%s anyone?" % T)
    check(56, "after the group is deleted, its handle no longer mentions anyone", s == 201 and mentions_of(d) == [],
          "status %s, mentions %s" % (s, mentions_of(d)))


def main():
    wd = threading.Timer(22, _time_is_up)
    wd.daemon = True
    wd.start()
    boot()
    for n, fn in ((1, stage1), (2, stage2), (3, stage3), (4, stage4), (5, stage5)):
        if n not in SELECTED:
            continue
        try:
            fn()
        except Exception as e:
            for r in STAGES[n]:
                bad(r, "not checked: %s" % e if isinstance(e, RuntimeError) else "check stopped (%s: %s)" % (type(e).__name__, e))
        for r in STAGES[n]:
            bad(r, "no result")  # only counts rules that were never decided
    with _lock:
        print("RESULT: %d passed, %d failed" % (passed, failed), flush=True)
    os._exit(0 if failed == 0 else 1)


main()
