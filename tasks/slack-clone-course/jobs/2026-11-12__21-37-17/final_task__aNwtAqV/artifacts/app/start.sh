#!/bin/sh
echo $$ >/tmp/start.pid
mkdir -p /app/data
pids=""
trap 'for p in $pids; do kill "$p" 2>/dev/null; done; exit 0' TERM INT
start_redis(){ redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no >/tmp/redis.log 2>&1 & pids="$pids $!"; }
start_node(){ PORT="$1" python3 /app/app.py >/tmp/node-$1.log 2>&1 & pids="$pids $!"; }
start_redis; start_node 8000; start_node 8001; start_node 8002
while :; do
 sleep 1
 for port in 6379 8000 8001 8002; do
  if ! (ss -ltnp 2>/dev/null | grep -q ":$port "); then
   case "$port" in
    6379) start_redis;;
    *) start_node "$port";;
   esac
  fi
 done
done
