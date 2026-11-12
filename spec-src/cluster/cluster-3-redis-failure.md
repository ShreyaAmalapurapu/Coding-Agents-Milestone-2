# Cluster.3: a killed Redis

Covers: writes, cross-node fan-out and a dense `event_id` while Redis is dead, Redis being
restarted by `/app/start.sh`, and normal fan-out after it is back.
Check: `bash /spec/cluster/checks/check_cluster_3.sh`. The rule numbers below match its output.
The check kills `redis-server` itself (by the PID that owns port `6379`), so run it as a command
of its own.

Do not start this spec until `check_cluster_2.sh` prints `RESULT: 7 passed, 0 failed`.

The conventions in `cluster-1-redis-fanout.md` apply to every rule.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | Used here for |
|---|---|
| `append_event` | Still gives dense ids with Redis gone, because the counter is in the shared database (see the Cluster.1 notes) |
| `broadcast` | Publishes on Redis when it is up |
| `/app/start.sh` supervisor | Restarts `redis-server` |

## Adds

| Function | What it does |
|---|---|
| `fallback_fanout()` | On every node, a loop (for example every 50-100 ms) that reads durable events newer than the last one it delivered from the shared event log and sends them to local subscribers. It covers events whose Redis message was lost. Each node delivers each `event_id` to a socket at most once |
| `redis_reconnect()` | When a publish or the subscription fails, stay up, retry every second, and resubscribe once Redis answers again |

## Rules (13-18)

The check subscribes a socket on `8002`, then sends `SIGKILL` to the process listening on `6379`.

13. **Writes keep working.** 12 messages posted one after another, rotating through `8000`,
    `8001`, `8002`, starting right after the kill, all return `201`, each within 5 seconds.
    A failed Redis publish must never fail the request.

14. **Delivery keeps working.** The socket on `8002` receives all 12 as `message.created`,
    within 5 seconds of the last post.

15. **Still dense.** Their `event_id`s are exactly `head + 1` to `head + 12`, in order: no gaps
    and no repeats, even though some were delivered without Redis.

16. **Redis comes back.** Within 10 seconds of the kill, `/app/start.sh` has started
    `redis-server` again and `6379` answers `PING` with `+PONG`.

17. **Fan-out resumes.** After that, a message posted on `8000` reaches sockets on `8001` and
    `8002` with the next `event_id` (`head + 13`), each exactly once.

18. **Nothing was lost.** `GET /api/channels/{id}/messages` on `8001` lists all 12 outage
    messages, newest first.

## Notes

- A socket must never get the same `event_id` twice, for example once through Redis and once
  through the fallback loop. Track the last `event_id` sent per (socket, channel).
- Connect to Redis with short timeouts (about 1 second) so a dead Redis cannot stall a request.
