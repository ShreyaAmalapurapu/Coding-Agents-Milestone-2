#!/usr/bin/env bash
# Runs rules 71-76 of the roles check (see roles-4-baseline.md): the four cluster baseline conditions.
# It kills the HTTP node on port 8001 (by the PID that owns the port) and waits for it to come back.
set -u
HERE="$(dirname "${BASH_SOURCE[0]}")"
# shellcheck disable=SC1091
source "$HERE/lib.sh"
N0=http://127.0.0.1:8000; N1=http://127.0.0.1:8001; N2=http://127.0.0.1:8002
BODY=/tmp/check_roles_4_body.json
PW="correct-horse-1"
RUN="$(printf '%x%x' "$(date +%s)" "$RANDOM")"
PY=/opt/test-venv/bin/python; [ -x "$PY" ] || PY=python3
T0=$SECONDS
pass=0; fail=0
ok()  { pass=$((pass+1)); return 0; }
bad() { say "FAIL $1 $2"; fail=$((fail+1)); return 0; }
finish() { echo "RESULT: $pass passed, $fail failed"; [ "$fail" -eq 0 ]; exit $?; }

# call <METHOD> <URL> [json-body] [token]  -> prints the HTTP status, saves the body in $BODY
call() {
  local args=(-s -o "$BODY" -w '%{http_code}' --max-time 3 -X "$1" "$2")
  [ -n "${3:-}" ] && args+=(-H 'Content-Type: application/json' -d "$3")
  [ -n "${4:-}" ] && args+=(-H "Authorization: Bearer $4")
  curl "${args[@]}" 2>/dev/null || true   # status 000 means no answer within 3s
}
# health <url>  -> prints "<status>/<status-field>/<node_id>" ("000//" when there is no answer)
health() {
  local s; : > "$BODY"; s=$(call GET "$1/api/health")
  printf '%s/%s' "$s" "$(python3 -c 'import json,sys
try:
    d=json.load(open(sys.argv[1])); n=d.get("node_id")
    print("%s/%s" % (d.get("status"), n if isinstance(n,int) and not isinstance(n,bool) else repr(n)))
except Exception: print("/")' "$BODY")"
}
healthy() {
  for u in $N0 $N1 $N2; do curl -fs --max-time 2 "$u/api/health" >/dev/null 2>&1 || return 1; done
}

# AIDEV-NOTE: boot wait is capped at 12s so boot + the 15s respawn wait still fit the 30s command limit.
if ! healthy; then
  # AIDEV-NOTE: redirect the whole subshell, not just start.sh: otherwise the subshell keeps this
  # check's stdout open for as long as the cluster runs, and a caller reading that output hangs.
  (cd /app && setsid nohup /app/start.sh </dev/null >/tmp/start.log 2>&1 &) </dev/null >/dev/null 2>&1
  for _ in $(seq 1 12); do healthy && break; sleep 1; done
fi
healthy || { say "FAIL 0 cluster is not answering /api/health on 8000, 8001, 8002 (see /tmp/start.log)"; echo "RESULT: 0 passed, 1 failed"; exit 1; }

# AIDEV-NOTE: BASELINE_KILL_ONLY=1 (used by the regression suite) runs only rules 75-76;
# BASELINE_NO_KILL=1 runs only rules 71-74.
if [ "${BASELINE_KILL_ONLY:-}" != 1 ]; then
# ===== 71-72: health and node ids =====
h0=$(health $N0)
if [ "$h0" = "200/ok/0" ]; then ok 71 "health on 8000 returns 200 with status ok and node_id 0"
else bad 71 "health on 8000: expected 200/ok/0 (status/status field/node_id), got $h0"; fi
got=""; for u in $N0 $N1 $N2; do got="$got $(health "$u")"; done
if [ "$got" = " 200/ok/0 200/ok/1 200/ok/2" ]; then ok 72 "ports 8000, 8001, 8002 report node_id 0, 1, 2"
else bad 72 "node ids: expected 200/ok/0 200/ok/1 200/ok/2, got$got"; fi

# ===== 73-74: WebSocket token check =====
call POST $N0/api/auth/register "{\"username\":\"bl$RUN\",\"password\":\"$PW\"}" >/dev/null
TOKEN=$(python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get("token") or "")
except Exception: pass' "$BODY")
ws=$(timeout 8 "$PY" - "$TOKEN" <<'PY' 2>/dev/null
import asyncio, sys
try:
    import websockets
except ImportError:
    print("nows\nnows"); sys.exit()
async def probe(token):
    # every stage has its own time limit, so a server that never answers is named, not just "error"
    try:
        ws = await asyncio.wait_for(websockets.connect("ws://127.0.0.1:8000/api/ws?token=" + token,
                                                       open_timeout=3, close_timeout=1), 3.5)
    except asyncio.TimeoutError:
        return "handshake-timeout"
    except Exception:
        return "refused"
    try:
        await asyncio.wait_for(ws.recv(), 1.5)
        r = "frame"          # something was sent: the connection is live
    except asyncio.TimeoutError:
        r = "open"           # handshake accepted and still open
    except Exception:
        r = "closed"
    try:
        await asyncio.wait_for(ws.close(), 1.2)
    except Exception:
        r += "+close-hung"
    return r
async def main():
    print(await probe("bogus-token-not-real-0000"), flush=True)
    print(await probe(sys.argv[1]) if sys.argv[1] else "notoken", flush=True)
asyncio.run(main())
PY
)
{ read -r wbad; read -r wgood; } <<<"$ws"
HUNG="the server did not answer the client's WebSocket close frame (opcode 8): reply with a close frame and end the connection"
case "${wbad:-}" in
  refused|closed|closed+close-hung) ok 73 "a WebSocket with a bad token is rejected" ;;
  nows) bad 73 "the python websockets package is not available to the check" ;;
  handshake-timeout) bad 73 "a WebSocket with a bad token: expected it refused (401) or closed, got no handshake answer within 3.5 s" ;;
  open*|frame*) bad 73 "a WebSocket with a bad token: expected it refused (401) or closed, got it accepted and open" ;;
  *) bad 73 "a WebSocket with a bad token: the probe did not finish within 8 s (the server held the connection open without answering)" ;;
esac
case "${wgood:-}" in
  open|frame) ok 74 "a WebSocket with a valid token opens and stays open" ;;
  open+close-hung|frame+close-hung) ok 74 "a WebSocket with a valid token opens and stays open"
    say "NOTE 74 passed, but $HUNG" ;;
  notoken) bad 74 "setup: could not register a user to get a token (Roles.1 rule 1 first)" ;;
  nows) bad 74 "the python websockets package is not available to the check" ;;
  handshake-timeout) bad 74 "a WebSocket with a valid token (?token=): expected it open, got no handshake answer within 3.5 s" ;;
  refused) bad 74 "a WebSocket with a valid token (?token=): expected it open, got the handshake refused (is the token read from the ?token= query?)" ;;
  closed*) bad 74 "a WebSocket with a valid token: expected it to stay open, but the server closed it within 1.5 s" ;;
  *) bad 74 "a WebSocket with a valid token: the probe did not finish within 8 s (${wbad:+bad-token probe: $wbad; }the server held a connection without answering)" ;;
esac

fi
[ "${BASELINE_NO_KILL:-}" = 1 ] && finish

# ===== 75-76: kill node 1 by the PID that owns port 8001 =====
# AIDEV-NOTE: never kill by name; only PIDs that own the listening socket on :8001, and never
# this script, its parent, or PID 1.
pids=$(ss -ltnpH 'sport = :8001' 2>/dev/null | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)
safe=""
for p in $pids; do
  [ "$p" = $$ ] || [ "$p" = "$PPID" ] || [ "$p" = 1 ] || safe="$safe $p"
done
if [ -z "$safe" ]; then
  bad 75 "could not find the PID that owns port 8001 with ss -ltnp"
  bad 76 "not checked: node 1 was not killed"
  finish
fi
kill -9 $safe 2>/dev/null
sleep 1
ok0=no; ok2=no; a=""; c=""
for _ in 1 2 3; do
  a=$(health $N0); c=$(health $N2)
  [ "${a%/*}" = "200/ok" ] && ok0=yes; [ "${c%/*}" = "200/ok" ] && ok2=yes
  [ $ok0 = yes ] && [ $ok2 = yes ] && break
  sleep 0.5
done
if [ $ok0 = yes ] && [ $ok2 = yes ]; then ok 75 "after killing the node on 8001, 8000 and 8002 still answer health with status ok"
else bad 75 "after SIGKILL of PID(s)$safe on 8001, expected 8000 and 8002 to answer 200/ok, got 8000 $a and 8002 $c (each node must be its own process)"; fi

limit=${BASELINE_RESPAWN_LIMIT:-$((T0 + 27 - SECONDS))}; [ "$limit" -gt 15 ] && limit=15
back=""; t=0
while [ "$t" -lt $((limit * 2)) ]; do
  b=$(health $N1)
  if [ "$b" = "200/ok/1" ]; then back=$((t / 2)); break; fi
  sleep 0.5; t=$((t + 1))
done
if [ -n "$back" ]; then ok 76 "node 1 came back by itself within ${back}s"
else
  bad 76 "node 1 did not come back on 8001 within ${limit}s (last health: $b); /app/start.sh must restart a killed node"
  say "NOTE: to restore the cluster, stop the PIDs on 8000 and 8002 (ss -ltnp) and start /app/start.sh again"
fi
finish
