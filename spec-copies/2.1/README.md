# Read this first: what to build in this run, in what order, and when you may submit

This run builds **one component: Features built on messages**, starting from an empty `/app`.
The task description is still the grading contract; `/spec` says what to work on now, in what order,
and how to prove it works. **Do not edit, move, or delete anything in `/spec`.** Write code only in `/app`.

## Order of work

For each step, in this order: read its spec file, build it, then run `bash /spec/<check>` until it
prints `RESULT: <rules> passed, 0 failed`.

| # | Step | Spec | Check | Rules | Done by (min) |
|---|---|---|---|---|---|
| 1 | Features.1 | `features/features-1-profiles-dms.md` | `features/checks/check_features_1.sh` | 13 | 10 |
| 2 | Features.2 | `features/features-2-reactions.md` | `features/checks/check_features_2.sh` | 10 | 18 |
| 3 | Features.3 | `features/features-3-files.md` | `features/checks/check_features_3.sh` | 8 | 24 |
| 4 | Features.4 | `features/features-4-slash-commands.md` | `features/checks/check_features_4.sh` | 11 | 33 |
| 5 | Features.5 | `features/features-5-groups-mentions.md` | `features/checks/check_features_5.sh` | 14 | 44 |

**Use the step runner: `bash /spec/step.sh`.** It runs the check of the current step (the first step
in the table that has not passed yet) and, at the end of its output, says whether the step passed and
which step to do next. Always work on the step it names; it moves on only when that step's check
passes, so never jump ahead to a later step or component. `bash /spec/step.sh --status` lists every
step and where you are. You may run any check directly to look at it, but only `step.sh` records progress.

After each step, rerun the earlier checks of the same component (`bash /spec/step.sh <#>` reruns step
`<#>`; if it no longer passes, it becomes the current step again).

**When a step is stuck.** If its check has not improved for 3 runs in a row, stop patching and find the
cause: (1) run `bash /spec/restart.sh`, because a node still running old code is the most common reason a
fix seems to do nothing; (2) re-read that rule in the spec file; (3) test the one failing request yourself with `curl`
and read the node's log in `/tmp`. After 6 runs of `step.sh` on the same step, if it still fails,
`bash /spec/step.sh --skip` moves on; come back to skipped steps at the end.

**Budget.** You have 50 minutes (`bash /app/timer.sh` shows what is left) and about 180 commands; the
commands usually run out first. Every command's output stays in your context, so combine edits, keep
output short (`| tail -n 30`, `sed -n` ranges), and never print a large file twice.

## Other files

- `step.sh`: the step runner (see [Order of work](#order-of-work)).
- `restart.sh`: restarts the cluster cleanly and waits until it is healthy (see [Working safely](#working-safely)).
- `features/checks/check_features_all.sh`: rules 1-56 in one command.
- `features/checks/lib.sh`, `features/checks/check_features_core.py`: used by the checks; do not run them.

Each spec file has **Uses** (functions from earlier specs: call them, do not rewrite them), **Adds**
(what it introduces, with the names to use) and numbered **Rules** (its check tests each by number).

**Do not read the source of the checks** (`*_core.py`, `*_core.sh`, `lib.sh`, `regression.sh`): the
spec files are the rules. Build from the specs and use the checks' output to find what is wrong.

## Scope

Do NOT start on these in this run, except where a rule here needs a piece of them: Roles and invitations; Channels and message history; Cluster robustness; IRC gateway, web UI and message edits.

Roles and Channels are not in this `/spec`. Build only the basics the steps need, as the task
description describes them (no roles, invitations, search, pins or read state unless a rule asks):
register/login/me (tokens valid on every node); `POST /api/workspaces` (creates `#general`, creator is
`owner`), `GET /api/workspaces`, `GET /api/workspaces/{slug}`, its `members`; creating and joining
channels, `GET /api/channels/{id}/members`; posting (`201 {message}`) and listing messages (newest
first); the WebSocket (`subscribe` → `subscribed` with `head_event_id`, `resume` → missed events then
`resumed`, durable frames with a dense per-channel `event_id` on every node).

## How to build the cluster

- **One shared store**: one SQLite file in `/app/data` (WAL, 5 s busy timeout) used by every process;
  no state only in one process's memory.
- **Events**: each durable event gets its `event_id` from a per-channel counter row, in the same
  transaction (`BEGIN IMMEDIATE`) that stores the change and the event row (kept for `resume`).
- **Fan-out**: publish committed events on Redis (`127.0.0.1:6379`) and also poll the events table every
  50-100 ms, so delivery survives a dead Redis. Send each `event_id` to a socket at most once.
- **`/app/start.sh`** stays in the foreground and supervises one process each for `redis-server`, the
  HTTP nodes on `8000`, `8001`, `8002`, restarting any that dies within a second or two. It writes
  its PID to `/tmp/start.pid` and stops its children on `SIGTERM`. List Python deps in `/app/requirements.txt`.

## Do not break the baseline

When you submit: (1) `GET /api/health` on `:8000` returns `200` `{"status": "ok", "node_id": 0}`;
(2) ports `8000`, `8001`, `8002` report `node_id` `0`, `1`, `2`; (3) a WebSocket with an invalid token
is rejected; (4) if one HTTP node is killed, the other two keep answering `/api/health` with `200`.

## Working safely

- Before a scripted edit, back the file up (`cp /app/x.py /tmp/x.py.bak`); afterwards run `python3 -m
  py_compile` and `wc -l`, and restore the backup if it shrank or broke. Prefer several small files.
- **Never stop processes by name** (`pkill -f`, `killall`, `pgrep -f`, parsing `ps`): that can kill you.
  Use a saved PID or the PID that owns a port (`ss -ltnp`).
- **Restart only with `bash /spec/restart.sh`**, as a command of its own, after every code change. It stops
  `/app/start.sh` and anything still holding the ports, starts it again and waits until all three nodes
  answer. Do not start `/app/start.sh` by hand a second time: the old nodes keep the ports (`address
  already in use` in `/tmp`) and every check then tests your old code.
- Run every check as a command of its own; never chain a restart, a `sleep` and a check (commands are
  cut off after 30 s). A check starts `/app/start.sh` if the cluster is down and uses random names, so
  you can rerun it freely. Each ends with a summary line such as `RESULT: 12 passed, 0 failed`.

## Before you submit

Run `bash /spec/step.sh --status`: every step must be `PASSED`, apart from steps you skipped and could
not fix in the time left.

Submit (`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`) only when, in output you saw after your last code
change, every check in the table ends with `RESULT: <rules> passed, 0 failed`, `check_features_all.sh` with `RESULT: 56 passed, 0 failed`, and the baseline holds.
If `bash /app/timer.sh` shows under 4 minutes and something still fails, stop adding features: restart
the cluster, confirm the baseline, and submit. These rules are necessary, not sufficient.
