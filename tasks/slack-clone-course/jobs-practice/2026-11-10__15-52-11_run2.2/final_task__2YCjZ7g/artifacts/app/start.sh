#!/bin/sh
echo $$ >/tmp/start.pid
mkdir -p /app/data
redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no >/tmp/redis.log 2>&1 & rp=$!
trap 'kill $rp $p0 $p1 $p2 2>/dev/null; exit 0' TERM INT
while :; do
 kill -0 $rp 2>/dev/null || { redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no >/tmp/redis.log 2>&1 & rp=$!; }
 for i in 0 1 2; do eval p=\$p$i; kill -0 "$p" 2>/dev/null || { PORT=$((8000+i)) python3 /app/app.py >/tmp/http$i.log 2>&1 & eval p$i=\$!; }; done
 sleep 1
done
