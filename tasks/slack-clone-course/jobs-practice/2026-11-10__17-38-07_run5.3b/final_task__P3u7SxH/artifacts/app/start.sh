#!/bin/sh
set -eu
mkdir -p /app
pids=""
irc_pid=""
cleanup(){ for p in $pids; do kill "$p" 2>/dev/null || true; done; [ -z "$irc_pid" ] || kill "$irc_pid" 2>/dev/null || true; }
trap cleanup INT TERM EXIT
while :; do
  if ! ss -ltn "sport = :6667" 2>/dev/null | grep -q LISTEN; then python3 /app/irc.py >/tmp/huddle-irc.log 2>&1 & irc_pid=$!; fi
  for n in 0 1 2; do
    port=$((8000+n))
    if ! ss -ltn "sport = :$port" 2>/dev/null | grep -q LISTEN; then
      python3 /app/server.py "$n" >/tmp/huddle-$n.log 2>&1 &
      pids="$pids $!"
    fi
  done
  sleep 1
done
