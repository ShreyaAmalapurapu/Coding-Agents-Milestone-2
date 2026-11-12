#!/usr/bin/env python3
"""Checks for component D (cluster robustness). Prints PASS/FAIL per rule.

CHECK_STAGE=1..4 runs one stage (one spec file). There is no "all": stages 2 and 3 kill a process
and wait for it to come back, so together they would not fit the agent's 30 second command limit.
Processes are only ever killed by the PID that owns a port (from `ss -ltnp`), never by name.
"""
import asyncio
import json
import os
import random
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

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

STAGES = {1: range(1, 6), 2: range(6, 13), 3: range(13, 19), 4: range(19, 22)}
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
            say("FAIL %s not reached: the check's time budget ran out (the cluster was slow or did not come back)" % r)
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
def call(method, url, body=None, token=None, timeout=5):
    """Returns (status, parsed-json-or-None). Status 0 means no answer."""
    left = DEADLINE - (time.time() - T0)
    if left <= 0:
        return 0, None
    timeout = min(timeout, max(left, 0.5))
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except Exception:
        return 0, None
    try:
        return status, (json.loads(raw) if raw else None)
    except Exception:
        return status, None


def dig(d, path):
    for k in path.split("."):
        if isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return None
    return d


def health(node, timeout=2):
    st, d = call("GET", node + "/api/health", timeout=timeout)
    return st, dig(d, "status"), dig(d, "node_id")


def healthy():
    return all(health(n)[0] == 200 for n in NODES)


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


# ---- processes: only by the PID that owns a port ----
def pids_on(port):
    try:
        out = subprocess.run(["ss", "-ltnpH", "sport = :%d" % port], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return []
    mine = {os.getpid(), os.getppid(), 1}
    return sorted({int(p) for p in re.findall(r"pid=(\d+)", out)} - mine)


def kill_port(port):
    pids = pids_on(port)
    for p in pids:
        try:
            os.kill(p, 9)
        except OSError:
            pass
    return pids


def redis_ping():
    try:
        with socket.create_connection(("127.0.0.1", 6379), timeout=1) as s:
            s.sendall(b"PING\r\n")
            return s.recv(64).startswith(b"+PONG")
    except OSError:
        return False


# ---- basics (from Specs A and B, or the basics the README asks for) ----
def reg(label):
    name = "%s%s" % (label, RUN)
    st, d = call("POST", N0 + "/api/auth/register", {"username": name, "password": PW})
    return {"name": name, "token": dig(d, "token"), "id": dig(d, "user.id")}


def setup(tag):
    """One user, one workspace, one fresh channel. Returns (user, channel_id)."""
    u = reg("d" + tag)
    if not u["token"]:
        raise RuntimeError("setup: could not register a user (register must work first)")
    slug = "d%s-%s" % (tag, RUN)
    call("POST", N0 + "/api/workspaces", {"slug": slug, "name": "Cluster " + slug}, u["token"])
    st, d = call("POST", "%s/api/workspaces/%s/channels" % (N0, slug), {"name": "c" + tag, "is_private": False}, u["token"])
    cid = dig(d, "channel.id")
    if cid is None:
        raise RuntimeError("setup: could not create a channel (status %s)" % st)
    return u, cid


def post(cid, u, body, node=N0, timeout=5):
    return call("POST", "%s/api/channels/%s/messages" % (node, cid), {"body": body}, u["token"], timeout=timeout)[0]


def bodies(cid, u, node):
    st, d = call("GET", "%s/api/channels/%s/messages?limit=50" % (node, cid), None, u["token"])
    return st, [m.get("body") for m in ((d or {}).get("messages") or []) if isinstance(m, dict)]


def frame_body(f):
    m = f.get("message")
    return m.get("body") if isinstance(m, dict) else f.get("body")


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
    f = await wait_for(ws, lambda f: f.get("type") == "subscribed" and str(f.get("channel_id")) == str(cid), timeout)
    return (f or {}).get("head_event_id")


async def created(ws, cid, n, timeout, after):
    """Collects up to n message.created frames of this channel with event_id > after."""
    got, end = [], time.time() + timeout
    while len(got) < n and time.time() < end:
        f = await wait_for(ws, lambda f: f.get("type") == "message.created" and str(f.get("channel_id")) == str(cid)
                           and isinstance(f.get("event_id"), int) and f["event_id"] > after, end - time.time())
        if f is None:
            break
        got.append(f)
    return got


async def in_thread(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def run_async(rules, coro_fn):
    if websockets is None:
        for r in rules:
            bad(r, "the python `websockets` package is not available to the check")
        return
    try:
        asyncio.run(asyncio.wait_for(coro_fn(), max(1, DEADLINE - (time.time() - T0))))
    except Exception as e:
        for r in rules:
            bad(r, "error or timeout (%s %s)" % (type(e).__name__, e))


# ===== D.1 Redis and cross-node fan-out (rules 1-5) =====
def stage1():
    check(1, "a Redis server answers PING on 127.0.0.1:6379", redis_ping(), "no +PONG from 127.0.0.1:6379 (start redis-server from /app/start.sh)")
    rp = set(pids_on(6379))
    hp = set(pids_on(8000) + pids_on(8001) + pids_on(8002))
    check(2, "Redis runs as its own process, separate from the HTTP nodes", rp and hp and not (rp & hp),
          "PIDs on 6379 %s, PIDs on 8000-8002 %s" % (sorted(rp), sorted(hp)))
    u, cid = setup("1")

    async def r3_5():
        socks = [await ws_open(p, u["token"]) for p in (8000, 8001, 8002)]
        try:
            heads = [await subscribe(s, cid) for s in socks]
            if not all(isinstance(h, int) for h in heads):
                for r in (3, 4, 5):
                    bad(r, "subscribe gave no head_event_id on every node (got %s)" % heads)
                return
            h = heads[0]
            tag = "fan " + RUN
            await in_thread(post, cid, u, tag, N1)
            got = [await created(s, cid, 1, 2, h) for s in socks]
            seen = [(g[0].get("event_id"), frame_body(g[0])) if g else None for g in got]
            check(3, "a message posted on 8001 reaches sockets on 8000, 8001 and 8002 within 2s, with the message in the frame",
                  all(x is not None and x[1] == tag for x in seen), "per node (event_id, message.body): %s" % seen)
            check(5, "every node reports the same event_id for the same message",
                  all(x is not None for x in seen) and len({x[0] for x in seen}) == 1 and seen[0][0] == h + 1,
                  "head %s, event_ids per node %s" % (h, [x and x[0] for x in seen]))

            def burst():
                with ThreadPoolExecutor(10) as ex:
                    return list(ex.map(lambda i: post(cid, u, "burst %d %s" % (i, RUN), NODES[i % 3]), range(30)))
            statuses = await in_thread(burst)
            frames = await created(socks[0], cid, 30, 4, h + 1)
            ids = sorted(f["event_id"] for f in frames)
            check(4, "30 concurrent posts across the 3 nodes all return 201 and give 30 consecutive event_ids, no gaps or repeats",
                  statuses.count(201) == 30 and ids == list(range(h + 2, h + 32)),
                  "statuses %s, %d frames, ids %s..%s (want %s..%s)" % (sorted(set(statuses)), len(ids), ids[:1], ids[-1:], h + 2, h + 31))
        finally:
            for s in socks:
                await s.close()
    run_async([3, 4, 5], r3_5)


# ===== D.2 A killed HTTP node (rules 6-12) =====
def stage2():
    u, cid = setup("2")

    async def body2():
        s1 = await ws_open(8001, u["token"])  # dies with node 1
        s2 = await ws_open(8002, u["token"])
        h = await subscribe(s1, cid)
        await subscribe(s2, cid)
        if not isinstance(h, int):
            for r in STAGES[2]:
                bad(r, "subscribe gave no head_event_id")
            return
        await in_thread(post, cid, u, "before " + RUN, N0)
        seen1 = await created(s1, cid, 1, 2, h)
        await created(s2, cid, 1, 2, h)
        last = seen1[0]["event_id"] if seen1 else h
        pids = kill_port(8001)
        if not pids:
            for r in STAGES[2]:
                bad(r, "could not find the PID that owns port 8001 with ss -ltnp")
            return
        await asyncio.sleep(0.5)
        hs = [health(N0), health(N2)]
        check(6, "after SIGKILL of the node on 8001, 8000 and 8002 answer health 200",
              all(x[0] == 200 and x[1] == "ok" for x in hs), "killed PID(s) %s; 8000 %s, 8002 %s (each node must be its own process)" % (pids, hs[0], hs[1]))
        a = await in_thread(post, cid, u, "during-a " + RUN, N0)
        b = await in_thread(post, cid, u, "during-b " + RUN, N2)
        check(7, "writes on 8000 and 8002 still return 201 while 8001 is down", (a, b) == (201, 201), "got %s and %s" % (a, b))
        want_b = ["during-b " + RUN, "during-a " + RUN, "before " + RUN]
        r0, r2 = bodies(cid, u, N0), bodies(cid, u, N2)
        check(8, "reads on 8000 and 8002 show the messages written before and during the outage",
              r0[0] == 200 and r2[0] == 200 and r0[1][:3] == want_b and r2[1][:3] == want_b,
              "8000 %s %s, 8002 %s %s" % (r0[0], r0[1][:3], r2[0], r2[1][:3]))
        got = await created(s2, cid, 2, 3, last)
        ids = [f["event_id"] for f in got]
        check(9, "a socket on 8002 receives both new messages with the next event_ids", ids == [last + 1, last + 2],
              "last seen %s, got %s" % (last, ids))
        await s1.close()
        s3 = await ws_open(8000, u["token"])
        try:
            await s3.send(json.dumps({"type": "resume", "channel_id": cid, "since_event_id": last}))
            seen, end = [], None
            stop = time.time() + 3
            while time.time() < stop:
                f = await wait_for(s3, lambda f: "event_id" in f or f.get("type") == "resumed", stop - time.time())
                if f is None:
                    break
                if f.get("type") == "resumed":
                    end = f
                    break
                seen.append(f.get("event_id"))
            check(10, "the client that was on 8001 resumes on 8000 and gets the missed events, then resumed",
                  seen == [last + 1, last + 2] and end is not None and end.get("head_event_id") == last + 2,
                  "since %s: replayed %s, resumed %s" % (last, seen, json.dumps(end)[:120]))
        finally:
            await s3.close()
        back = None
        t = time.time()
        while time.time() - t < 15 and time.time() - T0 < DEADLINE - 3:
            st, stt, nid = health(N1, timeout=1)
            if st == 200 and nid == 1:
                back = time.time() - t
                break
            await asyncio.sleep(0.5)
        check(11, "node 1 comes back on 8001 by itself (node_id 1) within 15s", back is not None,
              "no health 200 with node_id 1 on 8001 after %.0fs; /app/start.sh must restart a killed node" % (time.time() - t))
        if back is None:
            bad(12, "not checked: node 1 did not come back")
            say("NOTE: to restore the cluster, stop the PIDs on 8000 and 8002 (ss -ltnp) and start /app/start.sh again")
            await s2.close()
            return
        r1 = bodies(cid, u, N1)
        c = await in_thread(post, cid, u, "after " + RUN, N1)
        got = await created(s2, cid, 1, 3, last + 2)
        check(12, "the restarted node shows the outage writes, and its new post reaches 8002 with the next event_id",
              r1[0] == 200 and r1[1][:2] == want_b[:2] and c == 201 and [f["event_id"] for f in got] == [last + 3],
              "8001 list %s %s, post %s, frames %s%s" % (r1[0], r1[1][:2], c, [f["event_id"] for f in got],
                  " (the post through the restarted 8001 was stored but never reached the socket on 8002 within 3s:"
                  " the restarted node must publish its events again and other nodes must deliver them)" if c == 201 and not got else ""))
        await s2.close()
    run_async(list(STAGES[2]), body2)


# ===== D.3 A killed Redis (rules 13-18) =====
def stage3():
    u, cid = setup("3")
    if not redis_ping():
        for r in STAGES[3]:
            bad(r, "not checked: no Redis answering on 127.0.0.1:6379 (Cluster.1 rule 1 first)")
        return

    async def body3():
        sub = await ws_open(8002, u["token"])
        try:
            h = await subscribe(sub, cid)
            if not isinstance(h, int):
                for r in STAGES[3]:
                    bad(r, "subscribe gave no head_event_id")
                return
            pids = kill_port(6379)
            t_kill = time.time()
            if not pids:
                for r in STAGES[3]:
                    bad(r, "could not find the PID that owns port 6379 with ss -ltnp")
                return

            def writes():
                out = []
                for i in range(12):
                    out.append(post(cid, u, "outage %d %s" % (i, RUN), NODES[i % 3], timeout=5))
                    time.sleep(0.15)
                return out
            statuses = await in_thread(writes)
            check(13, "with Redis killed, 12 posts across all three nodes all return 201 (each within 5s)",
                  statuses == [201] * 12, "statuses %s" % statuses)
            got = await created(sub, cid, 12, 5, h)
            ids = [f["event_id"] for f in got]
            check(14, "a socket on 8002 still receives all 12 messages within 5s",
                  len(got) == 12, "received %d of 12 (event_ids %s)" % (len(got), ids))
            check(15, "event_ids stay dense across the outage: exactly head+1 to head+12 in order",
                  ids == list(range(h + 1, h + 13)), "head %s, ids %s" % (h, ids))
            back = None
            while time.time() - t_kill < 10:
                if redis_ping():
                    back = time.time() - t_kill
                    break
                await asyncio.sleep(0.3)
            check(16, "/app/start.sh restarts Redis: 6379 answers PING again within 10s of the kill",
                  back is not None, "no +PONG on 6379 %.0fs after the kill" % (time.time() - t_kill))
            await asyncio.sleep(0.5)
            sub1 = await ws_open(8001, u["token"])
            try:
                await subscribe(sub1, cid)
                st = await in_thread(post, cid, u, "after redis " + RUN, N0)
                g1 = await created(sub1, cid, 1, 2, h + 12)
                g2 = await created(sub, cid, 1, 2, h + 12)
                check(17, "after Redis is back, a post on 8000 reaches sockets on 8001 and 8002 with the next event_id",
                      st == 201 and [f["event_id"] for f in g1] == [h + 13] and [f["event_id"] for f in g2] == [h + 13],
                      "post %s, 8001 got %s, 8002 got %s" % (st, [f["event_id"] for f in g1], [f["event_id"] for f in g2]))
            finally:
                await sub1.close()
            r = bodies(cid, u, N1)
            want = ["outage %d %s" % (i, RUN) for i in range(11, -1, -1)]
            check(18, "the messages written during the outage are all listed on 8001, newest first",
                  r[0] == 200 and r[1][1:13] == want, "status %s, first bodies %s" % (r[0], r[1][:4]))
        finally:
            await sub.close()
    run_async(list(STAGES[3]), body3)


# ===== D.4 Performance (rules 19-21) =====
def stage4():
    u, cid = setup("4")
    WRITERS, PER = 3, 20

    async def body4():
        socks = [await ws_open(p, u["token"]) for p in (8000, 8000, 8001, 8001, 8002, 8002)]
        try:
            heads = [await subscribe(s, cid) for s in socks]
            if not all(isinstance(x, int) for x in heads):
                for r in STAGES[4]:
                    bad(r, "subscribe gave no head_event_id on every socket (got %s)" % heads)
                return
            sent, arrived = {}, {}
            stop = [False]

            async def reader(i, s):
                while not stop[0]:
                    try:
                        f = json.loads(await asyncio.wait_for(s.recv(), 0.5))
                    except asyncio.TimeoutError:
                        continue
                    except Exception:
                        return
                    if isinstance(f, dict) and f.get("type") == "message.created":
                        b = frame_body(f)
                        if isinstance(b, str) and b.startswith("perf "):
                            arrived.setdefault((i, b), time.time())

            def writer(w):
                for k in range(PER):
                    b = "perf %d-%d %s" % (w, k, RUN)
                    sent[b] = time.time()
                    post(cid, u, b, NODES[w % 3])
                    time.sleep(0.05)
            readers = [asyncio.create_task(reader(i, s)) for i, s in enumerate(socks)]
            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor(WRITERS) as ex:
                await asyncio.gather(*[loop.run_in_executor(ex, writer, w) for w in range(WRITERS)])
            await asyncio.sleep(2)
            stop[0] = True
            await asyncio.gather(*readers, return_exceptions=True)
            lat = [(arrived[(i, b)] - t) * 1000 for b, t in sent.items() for i in range(len(socks)) if (i, b) in arrived]
            expected = len(sent) * len(socks)
            missing = expected - len(lat)
            pct = 100.0 * missing / expected if expected else 100.0
            med = statistics.median(lat) if lat else float("inf")
            p95 = sorted(lat)[max(0, int(len(lat) * 0.95) - 1)] if lat else float("inf")
            check(19, "under concurrent writes from all 3 nodes, at most 1%% of expected message.created frames are missing",
                  pct <= 1.0, "%d of %d missing (%.1f%%)" % (missing, expected, pct))
            check(20, "median delivery latency is at most 300 ms", med <= 300, "median %.0f ms over %d deliveries" % (med, len(lat)))
            check(21, "p95 delivery latency is at most 800 ms", p95 <= 800, "p95 %.0f ms over %d deliveries" % (p95, len(lat)))
        finally:
            for s in socks:
                await s.close()
    run_async(list(STAGES[4]), body4)


def main():
    wd = threading.Timer(26, _time_is_up)
    wd.daemon = True
    wd.start()
    boot()
    for n, fn in ((1, stage1), (2, stage2), (3, stage3), (4, stage4)):
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
