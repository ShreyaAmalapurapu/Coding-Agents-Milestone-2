# Read this first: what to build in this run, in what order, and when you may submit

This run builds **5 components: Roles and invitations; Channels and message history; Cluster robustness; Features built on messages; IRC gateway, web UI and message edits**, starting from an empty `/app`.
The task description is still the grading contract; `/spec` says what to work on now, in what order,
and how to prove it works. **Do not edit, move, or delete anything in `/spec`.** Write code only in `/app`.

## Order of work

For each step, in this order: read its spec file, build it, then run `bash /spec/<check>` until it
prints `RESULT: <rules> passed, 0 failed`.

| # | Step | Spec | Check | Rules | Done by (min) |
|---|---|---|---|---|---|
| 1 | Roles.1 | `roles/roles-1-foundations.md` | `roles/checks/check_roles_1.sh` | 21 | 3 |
| 2 | Roles.2 | `roles/roles-2-roles.md` | `roles/checks/check_roles_2.sh` | 23 | 6 |
| 3 | Roles.3 | `roles/roles-3-invitations.md` | `roles/checks/check_roles_3.sh` | 26 | 10 |
| 4 | Roles.4 | `roles/roles-4-baseline.md` | `roles/checks/check_roles_4.sh` | 6 | 11 |
| 5 | Channels.1 | `channels/channels-1-management.md` | `channels/checks/check_channels_1.sh` | 15 | 13 |
| 6 | Channels.2 | `channels/channels-2-messages.md` | `channels/checks/check_channels_2.sh` | 24 | 16 |
| 7 | Channels.3 | `channels/channels-3-events.md` | `channels/checks/check_channels_3.sh` | 10 | 18 |
| 8 | Channels.4 | `channels/channels-4-mentions-read.md` | `channels/checks/check_channels_4.sh` | 17 | 20 |
| 9 | Cluster.1 | `cluster/cluster-1-redis-fanout.md` | `cluster/checks/check_cluster_1.sh` | 5 | 21 |
| 10 | Cluster.2 | `cluster/cluster-2-node-failure.md` | `cluster/checks/check_cluster_2.sh` | 7 | 22 |
| 11 | Cluster.3 | `cluster/cluster-3-redis-failure.md` | `cluster/checks/check_cluster_3.sh` | 6 | 24 |
| 12 | Cluster.4 | `cluster/cluster-4-performance.md` | `cluster/checks/check_cluster_4.sh` | 3 | 24 |
| 13 | Features.1 | `features/features-1-profiles-dms.md` | `features/checks/check_features_1.sh` | 13 | 26 |
| 14 | Features.2 | `features/features-2-reactions.md` | `features/checks/check_features_2.sh` | 10 | 27 |
| 15 | Features.3 | `features/features-3-files.md` | `features/checks/check_features_3.sh` | 8 | 28 |
| 16 | Features.4 | `features/features-4-slash-commands.md` | `features/checks/check_features_4.sh` | 11 | 30 |
| 17 | Features.5 | `features/features-5-groups-mentions.md` | `features/checks/check_features_5.sh` | 14 | 32 |
| 18 | IrcUi.1 | `irc-ui/irc-ui-1-edits.md` | `irc-ui/checks/check_irc_ui_1.sh` | 9 | 33 |
| 19 | IrcUi.2 | `irc-ui/irc-ui-2-irc-basics.md` | `irc-ui/checks/check_irc_ui_2.sh` | 8 | 34 |
| 20 | IrcUi.3 | `irc-ui/irc-ui-3-irc-bridge.md` | `irc-ui/checks/check_irc_ui_3.sh` | 8 | 35 |
| 21 | IrcUi.4 | `irc-ui/irc-ui-4-ui-markup.md` | `irc-ui/checks/check_irc_ui_4.sh` | 5 | 38 |
| 22 | IrcUi.5 | `irc-ui/irc-ui-5-ui-browser.md` | `irc-ui/checks/check_irc_ui_5.sh` | 7 | 40 |
| 23 | IrcUi.6 | `irc-ui/irc-ui-6-ui-messages.md` | `irc-ui/checks/check_irc_ui_6.sh` | 8 | 42 |
| 24 | IrcUi.7 | `irc-ui/irc-ui-7-ui-flows.md` | `irc-ui/checks/check_irc_ui_7.sh` | 8 | 44 |

**Use the step runner: `bash /spec/step.sh`.** It runs the check of the current step (the first step
in the table that has not passed yet) and, at the end of its output, says whether the step passed and
which step to do next. Always work on the step it names; it moves on only when that step's check
passes, so never jump ahead to a later step or component. `bash /spec/step.sh --status` lists every
step and where you are. You may run any check directly to look at it, but only `step.sh` records progress.

**After every step after Channels**, run `check_roles_all.sh`, `check_roles_4.sh` and `check_channels_all.sh`
(each as its own command). If one fails, you broke something that worked: fix it first.

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
- `roles/checks/check_roles_all.sh`: rules 1-70 in one command.
- `roles/checks/lib.sh`, `roles/checks/check_roles_core.sh`: used by the checks; do not run them.
- `channels/checks/check_channels_all.sh`: rules 1-66 in one command.
- `channels/checks/lib.sh`, `channels/checks/check_channels_core.py`: used by the checks; do not run them.
- `cluster/checks/lib.sh`, `cluster/checks/check_cluster_core.py`: used by the checks; do not run them.
- `features/checks/check_features_all.sh`: rules 1-56 in one command.
- `features/checks/lib.sh`, `features/checks/check_features_core.py`: used by the checks; do not run them.
- `irc-ui/checks/lib.sh`, `irc-ui/checks/check_irc_ui_core.py`: used by the checks; do not run them.

Each spec file has **Uses** (functions from earlier specs: call them, do not rewrite them), **Adds**
(what it introduces, with the names to use) and numbered **Rules** (its check tests each by number).

**Do not read the source of the checks** (`*_core.py`, `*_core.sh`, `lib.sh`, `regression.sh`): the
spec files are the rules. Build from the specs and use the checks' output to find what is wrong.

## Scope

- IRC bridging while a node is dead is built and checked with the IRC gateway (IrcUi rule 25).
- The web UI also calls threads, reactions, invitations, member roles and channel settings; build any that no other step provides, as the task description describes them.
- Reactions and `/invite` are built in Features; the UI only calls them.
- Cluster comes before Features and IRC/UI: its restarts, Redis fallback and fan-out shape every other part and are much harder to add at the end.

## How to build the cluster

- **One shared store**: one SQLite file in `/app/data` (WAL, 5 s busy timeout) used by every process;
  no state only in one process's memory.
- **Events**: each durable event gets its `event_id` from a per-channel counter row, in the same
  transaction (`BEGIN IMMEDIATE`) that stores the change and the event row (kept for `resume`).
- **Fan-out**: publish committed events on Redis (`127.0.0.1:6379`) and also poll the events table every
  50-100 ms, so delivery survives a dead Redis. Send each `event_id` to a socket at most once.
- **`/app/start.sh`** stays in the foreground and supervises one process each for `redis-server`, the
  HTTP nodes on `8000`, `8001`, `8002` and the IRC gateway on `6667`, restarting any that dies within a second or two. It writes
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
- `cluster/checks/check_cluster_2.sh`, `cluster/checks/check_cluster_3.sh`, `irc-ui/checks/check_irc_ui_3.sh`, `roles/checks/check_roles_4.sh` kill a process **on purpose** and wait for `/app/start.sh` to bring it back.
- Run every check as a command of its own; never chain a restart, a `sleep` and a check (commands are
  cut off after 30 s). A check starts `/app/start.sh` if the cluster is down and uses random names, so
  you can rerun it freely. Each ends with a summary line such as `RESULT: 12 passed, 0 failed`.

## Before you submit

Run `bash /spec/step.sh --status`: every step must be `PASSED`, apart from steps you skipped and could
not fix in the time left.

Submit (`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`) only when, in output you saw after your last code
change, every check in the table ends with `RESULT: <rules> passed, 0 failed`, `check_roles_all.sh` with `RESULT: 70 passed, 0 failed`, `check_channels_all.sh` with `RESULT: 66 passed, 0 failed`, `check_features_all.sh` with `RESULT: 56 passed, 0 failed`, and the baseline holds.
If `bash /app/timer.sh` shows under 4 minutes and something still fails, stop adding features: restart
the cluster, confirm the baseline, and submit. These rules are necessary, not sufficient.
