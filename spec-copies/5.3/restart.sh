#!/usr/bin/env bash
# Restarts the cluster cleanly, so the nodes run the code that is in /app now:
# stops /app/start.sh (its saved PID in /tmp/start.pid) and every process still listening on the
# cluster's ports, starts /app/start.sh again, and waits until 8000, 8001 and 8002 answer /api/health.
# It stops processes only by a saved PID or by the PID that owns a port, never by name.
set -u
PORTS="8000 8001 8002 6667 6379"
owners() {
  local p
  for p in $PORTS; do ss -ltnpH "sport = :$p" 2>/dev/null | grep -oE 'pid=[0-9]+' | cut -d= -f2; done | sort -u | grep -vx "$$"
}
healthy() {
  local p
  for p in 8000 8001 8002; do curl -fs --max-time 1 "http://127.0.0.1:$p/api/health" >/dev/null 2>&1 || return 1; done
}
ppid() { awk '/^PPid:/{print $2}' "/proc/$1/status" 2>/dev/null || echo 1; }
pgid() { awk '{print $5}' "/proc/$1/stat" 2>/dev/null; }
is_start() { tr '\0' ' ' <"/proc/$1/cmdline" 2>/dev/null | grep -q 'start\.sh'; }
# this script's own ancestors and their process groups are never stopped
mine=" $$ "; q=$$
while [ "${q:-1}" -gt 1 ] 2>/dev/null; do q=$(ppid "$q"); mine="$mine$q "; done
mypg=" "; for a in $mine; do mypg="$mypg$(pgid "$a") "; done
# stop <pid>: stops the process with its whole process group (a supervisor's restart loops and
# nodes), unless that group is this script's own; then only the process itself
stop() {
  local g; g=$(pgid "$1")
  case "$mine" in *" $1 "*) return ;; esac
  if [ -n "$g" ] && [ "$g" -gt 1 ] && case "$mypg" in *" $g "*) false ;; *) true ;; esac; then
    kill -- "-$g" 2>/dev/null
  else
    kill "$1" 2>/dev/null
  fi
}
# the topmost start.sh above each port owner (walking up from the owner's PID), so an old supervisor
# is stopped even if it saved its PID somewhere other than /tmp/start.pid
supervisors() {
  local pid q top n
  for pid in $(owners); do
    q=$pid; top=""
    for n in 1 2 3 4 5 6; do
      q=$(ppid "$q")
      [ "${q:-1}" -le 1 ] && break
      case "$mine" in *" $q "*) break ;; esac
      is_start "$q" && top=$q
    done
    [ -n "$top" ] && echo "$top"
  done | sort -u
}
sup="$(supervisors | tr '\n' ' ')"
[ -f /tmp/start.pid ] && sup="$sup $(cat /tmp/start.pid)"
for pid in $sup; do stop "$pid"; done
for _ in 1 2 3 4 5 6; do [ -z "$(owners)" ] && break; sleep 0.5; done
left=$(owners | tr '\n' ' ')
if [ -n "$left" ]; then
  echo "still listening after stopping /app/start.sh (old processes): $left- stopping them"
  for pid in $left; do stop "$pid"; done; sleep 1
  left=$(owners | tr '\n' ' ')
  [ -n "$left" ] && { kill -9 $left 2>/dev/null; sleep 0.5; }
fi
(cd /app && setsid nohup /app/start.sh </dev/null >/tmp/start.log 2>&1 &) </dev/null >/dev/null 2>&1
for _ in $(seq 1 30); do healthy && break; sleep 0.5; done
if healthy; then
  echo "RESTARTED: 8000, 8001 and 8002 answer /api/health"
else
  echo "NOT HEALTHY 15 s after starting /app/start.sh. Last lines of /tmp/start.log:"
  tail -n 8 /tmp/start.log 2>/dev/null
  exit 1
fi
