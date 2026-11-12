# Cluster.1: Redis and cross-node fan-out

Covers: the local Redis pub/sub daemon on `127.0.0.1:6379`, delivery of durable events to
sockets on every node, and a dense `event_id` under concurrent writes from all nodes.
Check: `bash /spec/cluster/checks/check_cluster_1.sh`. The rule numbers below match its output.

Your service is launched with `/app/start.sh`, which must stay in the foreground and keep the
cluster alive. The three HTTP nodes listen on `127.0.0.1:8000`, `:8001`, and `:8002`. State
written through one node must be visible through the other two.

Conventions for every rule (in all four Cluster specs): JSON in, JSON out; responses use one
top-level resource key; `Authorization: Bearer <token>` on every route, and a missing or invalid
token is `401`; an unknown id is `404`; every request must answer within 5 seconds.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

If Specs Roles and Channels are in `/spec`, these come from there. If they are not, build only
these basics first (see `instruction.md` for each route), and nothing else from those specs:

| Function | The basic it provides |
|---|---|
| `create_user`, `issue_token`, `user_from_token` | `POST /api/auth/register`, `GET /api/auth/me` |
| `create_workspace`, `create_channel` | `POST /api/workspaces`, `POST /api/workspaces/{slug}/channels` |
| `post_message`, `list_messages` | `POST /api/channels/{id}/messages` (`201 {message}`), `GET /api/channels/{id}/messages` newest first |
| `ws_endpoint`, `append_event`, `events_since` | `ws://.../api/ws?token=`; `subscribe` answered by `{"type":"subscribed","channel_id","head_event_id"}`; `resume` with `since_event_id` replays the missed events in order, then sends `{"type":"resumed","channel_id","head_event_id"}` |

## Adds

| Function | What it does |
|---|---|
| `/app/start.sh` supervisor | Starts `redis-server` on `127.0.0.1:6379` (for example `redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no`) and the three HTTP nodes, **each as its own process**, and restarts any of them that dies |
| `broadcast(channel_id, frame)` | Publishes a committed durable frame on Redis pub/sub; every node delivers it to its local subscribers of that channel |

## Rules (1-5)

1. **Redis is running.** Something answers the Redis `PING` command with `+PONG` on
   `127.0.0.1:6379`.

2. **Its own process.** The process listening on `6379` is not one of the processes listening
   on `8000`, `8001`, `8002` (use the real `redis-server`, not an in-process imitation).

3. **Fan-out.** A message posted on `8001` reaches subscribers on `8000`, `8001`, and `8002`
   within 2 seconds as `{"type": "message.created", "event_id", "channel_id", "message": <MessageObj>}`.
   The frame carries the full message under `message`.

4. **Dense under load.** 30 messages posted at the same time through all three nodes (10 at
   once) all return `201`, and a subscriber receives 30 `message.created` frames whose
   `event_id`s are consecutive: no gaps, no repeats.

5. **Same ids everywhere.** Subscribers on different nodes see the same `event_id` for the same
   message, and it is the channel's `head_event_id + 1`.

## Notes

- **Do not keep the event counter or the event log only in Redis.** Cluster.3 kills Redis while
  messages are being written. Give each event its `event_id` inside the same database
  transaction that stores it (for example a per-channel counter row in a shared SQLite file in
  WAL mode, updated under `BEGIN IMMEDIATE`). Use Redis only to tell other nodes that new events
  exist.
- Never stop processes by name (`pkill -f`, `killall`, `pgrep -f`): that can kill you. Find the
  PID that owns a port with `ss -ltnp` and stop that PID.
