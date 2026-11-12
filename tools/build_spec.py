#!/usr/bin/env python3
"""Builds milestone-2-specs/ (the agent's /spec) for one run of the run plan, with its README.

Usage:  python3 tools/build_spec.py <run> [--save]
        <run> is one of 1.1 1.2 1.3 2.1 2.2 2.3 3 4 5.1 5.2 5.3
        --save also copies the built folder to spec-copies/<run>/ (required for the submission)

The master specs and checks live in spec-src/ (outside the folder the agent sees). Every README
comes from the one template below, so the text is word for word the same across runs except the
parts the run requires: the file list, the scope, the order of work, how A and B are checked,
and the submit conditions. The testing mode is set only in each component's checks/lib.sh and is
never mentioned in the README.
"""
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "spec-src")
DEST = os.path.join(ROOT, "tasks/slack-clone-course/course-staff-files/final_task/environment/milestone-specs/milestone-2-specs")
COPIES = os.path.join(ROOT, "spec-copies")

# (spec file, title, rules, rule count, check)
COMP = {
    "A": {"key": "roles", "name": "Roles and invitations", "all": ("check_roles_all.sh", 70, "rules 1-70"),
          "core": "check_roles_core.sh", "steps": [
              ("roles-1-foundations.md", "Roles.1: auth, workspaces, joining channels", "1-19, 63, 70", 21, "check_roles_1.sh"),
              ("roles-2-roles.md", "Roles.2: roles, ownership transfer, role-based permissions", "20-40, 64, 65", 23, "check_roles_2.sh"),
              ("roles-3-invitations.md", "Roles.3: invitations and join mode", "41-62, 66-69", 26, "check_roles_3.sh"),
              ("roles-4-baseline.md", "Roles.4: the cluster baseline", "71-76", 6, "check_roles_4.sh")]},
    "B": {"key": "channels", "name": "Channels and message history", "all": ("check_channels_all.sh", 66, "rules 1-66"),
          "core": "check_channels_core.py", "steps": [
              ("channels-1-management.md", "Channels.1: channel management", "1-14, 66", 15, "check_channels_1.sh"),
              ("channels-2-messages.md", "Channels.2: messages, paging, search, pins", "15-31, 52-55, 63-65", 24, "check_channels_2.sh"),
              ("channels-3-events.md", "Channels.3: WebSocket events", "32-39, 61, 62", 10, "check_channels_3.sh"),
              ("channels-4-mentions-read.md", "Channels.4: mentions and read state", "40-51, 56-60", 17, "check_channels_4.sh")]},
    "C": {"key": "features", "name": "Features built on messages", "all": ("check_features_all.sh", 56, "rules 1-56"),
          "core": "check_features_core.py", "steps": [
              ("features-1-profiles-dms.md", "Features.1: profiles and direct messages", "1-13", 13, "check_features_1.sh"),
              ("features-2-reactions.md", "Features.2: reactions", "14-23", 10, "check_features_2.sh"),
              ("features-3-files.md", "Features.3: files", "24-31", 8, "check_features_3.sh"),
              ("features-4-slash-commands.md", "Features.4: slash commands", "32-42", 11, "check_features_4.sh"),
              ("features-5-groups-mentions.md", "Features.5: user groups, @group-handle and @here", "43-56", 14, "check_features_5.sh")]},
    "D": {"key": "cluster", "name": "Cluster robustness", "all": None, "core": "check_cluster_core.py", "steps": [
              ("cluster-1-redis-fanout.md", "Cluster.1: Redis and cross-node fan-out", "1-5", 5, "check_cluster_1.sh"),
              ("cluster-2-node-failure.md", "Cluster.2: a killed HTTP node (kills the node on 8001)", "6-12", 7, "check_cluster_2.sh"),
              ("cluster-3-redis-failure.md", "Cluster.3: a killed Redis (kills redis-server)", "13-18", 6, "check_cluster_3.sh"),
              ("cluster-4-performance.md", "Cluster.4: delivery speed under load", "19-21", 3, "check_cluster_4.sh")]},
    "E": {"key": "irc-ui", "name": "IRC gateway, web UI and message edits", "all": None, "core": "check_irc_ui_core.py", "steps": [
              ("irc-ui-1-edits.md", "IrcUi.1: editing and deleting messages", "1-9", 9, "check_irc_ui_1.sh"),
              ("irc-ui-2-irc-basics.md", "IrcUi.2: the IRC gateway, registration and commands", "10-17", 8, "check_irc_ui_2.sh"),
              ("irc-ui-3-irc-bridge.md", "IrcUi.3: IRC channels and bridging (kills the node on 8001)", "18-25", 8, "check_irc_ui_3.sh"),
              ("irc-ui-4-ui-markup.md", "IrcUi.4: the web UI page and its pinned hooks", "26-30", 5, "check_irc_ui_4.sh"),
              ("irc-ui-5-ui-browser.md", "IrcUi.5: the web UI in a browser", "31-37", 7, "check_irc_ui_5.sh"),
              ("irc-ui-6-ui-messages.md", "IrcUi.6: message actions in the browser", "38-45", 8, "check_irc_ui_6.sh"),
              ("irc-ui-7-ui-flows.md", "IrcUi.7: workspace, channel and account flows in the browser", "46-53", 8, "check_irc_ui_7.sh")]},
}
# How much of the 50 minutes a step deserves, relative to its rule count (the UI steps are heavier).
WEIGHT = {"irc-ui-4-ui-markup.md": 3, "irc-ui-5-ui-browser.md": 2, "irc-ui-6-ui-messages.md": 2, "irc-ui-7-ui-flows.md": 2,
          "cluster-1-redis-fanout.md": 2, "cluster-3-redis-failure.md": 1.5}

# ab: how Roles (A) and Channels (B) are given. None: not in /spec. "regression": only the suite.
# "specs+regression": spec files plus the suite. "full": spec files and their plain checks.
RUNS = {
    "1.1": {"comps": "C", "mode": "score_only", "ab": None},
    "1.2": {"comps": "D", "mode": "score_only", "ab": None},
    "1.3": {"comps": "E", "mode": "score_only", "ab": None},
    "2.1": {"comps": "C", "mode": "verbose", "ab": None},
    "2.2": {"comps": "D", "mode": "verbose", "ab": None},
    "2.3": {"comps": "E", "mode": "verbose", "ab": None},
    "3": {"comps": "DCE", "mode": "verbose", "ab": None},
    "4": {"comps": "DCE", "mode": "verbose", "ab": None, "checks_only": True},
    "5.1": {"comps": "DCE", "mode": "verbose", "ab": "regression"},
    "5.2": {"comps": "DCE", "mode": "verbose", "ab": "specs+regression"},
    "5.3": {"comps": "DCE", "mode": "verbose", "ab": "full"},
}
REGRESSION = os.path.join(SRC, "regression", "regression.sh")
KILLERS = {"check_roles_4.sh", "check_cluster_2.sh", "check_cluster_3.sh", "check_irc_ui_3.sh"}


def code(s):
    return "`%s`" % s


# ---------------------------------------------------------------- the ordered steps of a run
def plan(run):
    """The steps of a run, in order. One list feeds both the README table and /spec/step.sh.

    crit is how step.sh decides a step passed: "result" (the check's last RESULT line has 0 failed),
    "reg0" (REGRESSION: 0 broken) or "regcomp:<key>" (the suite ran and lists nothing broken for <key>).
    """
    cfg = RUNS[run]
    ab = cfg["ab"]
    new = list(cfg["comps"])
    specs = not cfg.get("checks_only")
    has_ab_specs = ab in ("specs+regression", "full")
    rows = []

    def row(step, spec_path, spec_text, check, target, weight, crit):
        rows.append({"step": step, "spec_path": spec_path, "spec_text": spec_text, "check": check,
                     "target": target, "weight": weight, "crit": crit})

    if ab == "regression":
        row("Roles + Channels", None, "(none: build them from the task description)", "checks/regression.sh",
            "0 broken", 30, "reg0")
    for c in (["A", "B"] if ab else []) + new:
        cm = COMP[c]
        k = cm["key"]
        if c in "AB" and ab == "regression":
            continue
        for f, title, rules, n, chk in cm["steps"]:
            step = title.split(":")[0]
            path = "%s/%s" % (k, f) if ((c in new and specs) or (c in "AB" and has_ab_specs)) else None
            text = "`%s`" % path if path else "(none)"
            if c in "AB" and ab == "specs+regression":
                row(step, path, text, "checks/regression.sh", "its `%s` rules fixed" % k, n, "regcomp:" + k)
            else:
                row(step, path, text, "%s/checks/%s" % (k, chk), "%d" % n, n * WEIGHT.get(f, 1), "result")
        if c == "B" and ab == "specs+regression":
            row("Roles + Channels", None, "(all of the above)", "checks/regression.sh", "0 broken", 4, "reg0")
    return rows


STEP_SH = r'''#!/usr/bin/env bash
# The step runner for this /spec. It keeps track of which step of the README's "Order of work" table
# you are on, runs that step's check, and says at the end whether it passed and what to do next.
#   bash /spec/step.sh            run the check of the current step (the first one not passed yet)
#   bash /spec/step.sh <#>        rerun step <#> (for example an earlier or a skipped step)
#   bash /spec/step.sh --status   list every step and its state; runs no check
#   bash /spec/step.sh --skip     give up on the current step for now (only after 6 runs of it)
# Progress is kept in /tmp/spec-steps. The check's own output is printed unchanged.
set -u
S=/tmp/spec-steps
mkdir -p "$S"
STEPS=(
@@STEPS@@
)
N=${#STEPS[@]}
MIN_TRIES_TO_SKIP=6

get() { IFS='|' read -r NAME SPEC CHECK CRIT <<<"${STEPS[$(($1 - 1))]}"; }
tries() { cat "$S/$1.tries" 2>/dev/null || echo 0; }
state() { if [ -f "$S/$1.pass" ]; then echo PASSED; elif [ -f "$S/$1.skip" ]; then echo SKIPPED; else echo "not passed"; fi; }
current() {
  local i
  for ((i = 1; i <= N; i++)); do
    [ -f "$S/$i.pass" ] || [ -f "$S/$i.skip" ] || { echo "$i"; return; }
  done
  echo 0
}
passed() {  # passed <crit> <output file>
  case "$1" in
    result) grep -E '^RESULT: ' "$2" | tail -n 1 | grep -qE '^RESULT: [1-9][0-9]* passed, 0 failed' ;;
    reg0) grep -q '^REGRESSION: 0 broken' "$2" ;;
    regcomp:*) grep -q '^REGRESSION: ' "$2" && ! grep -qE "^REGRESSION FAIL (${1#regcomp:}|cluster) " "$2" ;;
    *) return 1 ;;
  esac
}
status() {
  local i
  for ((i = 1; i <= N; i++)); do
    get "$i"
    printf '%3d. %-14s %-11s (%s runs)  bash /spec/%s\n' "$i" "$NAME" "$(state "$i")" "$(tries "$i")" "$CHECK"
  done
}
next_msg() {
  local c skipped="" i
  c=$(current)
  if [ "$c" = 0 ]; then
    for ((i = 1; i <= N; i++)); do [ -f "$S/$i.skip" ] && skipped="$skipped $i"; done
    if [ -n "$skipped" ]; then
      echo "ALL STEPS DONE EXCEPT THE SKIPPED ONES:$skipped. If time is left, go back to each with: bash /spec/step.sh <#>"
    else
      echo "ALL STEPS PASSED. Now follow \"Before you submit\" in /spec/README.md."
    fi
    return
  fi
  get "$c"
  if [ "$SPEC" = "-" ]; then
    echo "NEXT: step $c/$N, $NAME. Build what bash /spec/$CHECK tests, then run: bash /spec/step.sh"
  else
    echo "NEXT: step $c/$N, $NAME. Read /spec/$SPEC, build it, then run: bash /spec/step.sh"
  fi
}

arg="${1:-}"
case "$arg" in
  --status) status; echo; next_msg; exit 0 ;;
  --skip)
    i=$(current)
    [ "$i" = 0 ] && { next_msg; exit 0; }
    get "$i"
    if [ "$(tries "$i")" -lt "$MIN_TRIES_TO_SKIP" ]; then
      echo "STEP $i ($NAME) has been run $(tries "$i") times; you may skip it only after $MIN_TRIES_TO_SKIP runs of bash /spec/step.sh. Keep fixing it."
      exit 1
    fi
    touch "$S/$i.skip"
    echo "STEP $i ($NAME) SKIPPED. Later steps may depend on it; come back at the end with: bash /spec/step.sh $i"
    next_msg
    exit 0 ;;
  "") i=$(current); [ "$i" = 0 ] && { next_msg; exit 0; } ;;
  *)
    if ! [[ "$arg" =~ ^[0-9]+$ ]] || [ "$arg" -lt 1 ] || [ "$arg" -gt "$N" ]; then
      echo "usage: bash /spec/step.sh [<step number 1-$N> | --status | --skip]"
      exit 2
    fi
    i=$arg ;;
esac

get "$i"
echo "STEP $i/$N: $NAME (bash /spec/$CHECK)"
out=$(mktemp /tmp/spec-step.XXXXXX)
bash "/spec/$CHECK" 2>&1 | tee "$out"
rc=${PIPESTATUS[0]}
echo $(($(tries "$i") + 1)) >"$S/$i.tries"
if passed "$CRIT" "$out"; then
  touch "$S/$i.pass"
  rm -f "$S/$i.skip"
  # Following steps judged by the same command and the same test passed with this very output.
  for ((j = i + 1; j <= N; j++)); do
    a="${STEPS[$((j - 1))]}"; b="${STEPS[$((i - 1))]}"
    [ "${a#*|*|}" = "${b#*|*|}" ] || break
    touch "$S/$j.pass"; rm -f "$S/$j.skip"
  done
  echo "STEP $i ($NAME) PASSED."
  next_msg
else
  was=""
  [ -f "$S/$i.pass" ] && was=" It passed before, so a later change broke it: fix it before anything else."
  rm -f "$S/$i.pass"
  if [ "$i" = "$(current)" ]; then again="bash /spec/step.sh"; else again="bash /spec/step.sh $i"; fi
  echo "STEP $i ($NAME) NOT PASSED YET (run $(tries "$i")).$was Fix what fails, then run: $again"
  if [ "$i" = "$(current)" ] && [ "$(tries "$i")" -ge "$MIN_TRIES_TO_SKIP" ]; then
    echo "If you cannot make progress on it, you may move on with: bash /spec/step.sh --skip"
  fi
fi
rm -f "$out"
exit "$rc"
'''

RESTART_SH = r'''#!/usr/bin/env bash
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
'''


def step_sh(run):
    lines = ['"%s|%s|%s|%s"' % (r["step"], r["spec_path"] or "-", r["check"], r["crit"]) for r in plan(run)]
    return STEP_SH.replace("@@STEPS@@", "\n".join(lines))


# ---------------------------------------------------------------- README template
def readme(run):
    cfg = RUNS[run]
    ab = cfg["ab"]
    new = list(cfg["comps"])
    has_ab_specs = ab in ("specs+regression", "full")
    has_ab_checks = ab == "full"
    has_reg = ab in ("regression", "specs+regression")
    specs = not cfg.get("checks_only")
    built = (["A", "B"] if ab else []) + new
    L = []
    w = L.append

    # -- 1. title and what this run is
    names = [COMP[c]["name"] for c in built]
    w("# Read this first: what to build in this run, in what order, and when you may submit\n")
    if len(names) == 1:
        w("This run builds **one component: %s**, starting from an empty `/app`." % names[0])
    else:
        w("This run builds **%d components: %s**, starting from an empty `/app`." % (len(names), "; ".join(names)))
    w("The task description is still the grading contract; `/spec` says what to work on now, in what order,")
    w("and how to prove it works. **Do not edit, move, or delete anything in `/spec`.** Write code only in `/app`.\n")

    # -- 2. order of work (first, so it is never missed)
    w("## Order of work\n")
    if specs or has_ab_specs:
        w("For each step, in this order: read its spec file, build it, then run `bash /spec/<check>` until it")
        w("prints `RESULT: <rules> passed, 0 failed`.%s\n" % ("" if specs else " Steps with no spec file are specified by their check alone."))
    else:
        w("For each step, in this order: run `bash /spec/<check>` and build until it prints")
        w("`RESULT: <rules> passed, 0 failed`. There are no spec files in this run (see [Files](#other-files)).\n")
    rows = plan(run)
    total = sum(r["weight"] for r in rows)
    w("| # | Step | Spec | Check | Rules | Done by (min) |")
    w("|---|---|---|---|---|---|")
    t = 0.0
    for i, r in enumerate(rows, 1):
        t += r["weight"]
        w("| %d | %s | %s | `%s` | %s | %d |" % (i, r["step"], r["spec_text"], r["check"], r["target"],
                                                max(1, round(44 * t / total))))
    w("")
    w("**Use the step runner: `bash /spec/step.sh`.** It runs the check of the current step (the first step")
    w("in the table that has not passed yet) and, at the end of its output, says whether the step passed and")
    w("which step to do next. Always work on the step it names; it moves on only when that step's check")
    w("passes, so never jump ahead to a later step or component. `bash /spec/step.sh --status` lists every")
    w("step and where you are. You may run any check directly to look at it, but only `step.sh` records progress.\n")
    if has_reg:
        w("**After every step**, run `bash /spec/checks/regression.sh`. While Roles and Channels are unfinished it")
        w("lists what is left; after that, any `REGRESSION FAIL` line means you broke something: fix it first.\n")
    elif has_ab_checks:
        w("**After every step after Channels**, run `check_roles_all.sh`, `check_roles_4.sh` and `check_channels_all.sh`")
        w("(each as its own command). If one fails, you broke something that worked: fix it first.\n")
    w("After each step, rerun the earlier checks of the same component (`bash /spec/step.sh <#>` reruns step")
    w("`<#>`; if it no longer passes, it becomes the current step again).\n")
    w("**When a step is stuck.** If its check has not improved for 3 runs in a row, stop patching and find the")
    w("cause: (1) run `bash /spec/restart.sh`, because a node still running old code is the most common reason a")
    w("fix seems to do nothing; (2) %s; (3) test the one failing request yourself with `curl`" %
      ("re-read that rule in the spec file" if (specs or has_ab_specs) else "re-read what that rule's check sends"))
    w("and read the node's log in `/tmp`. After 6 runs of `step.sh` on the same step, if it still fails,")
    w("`bash /spec/step.sh --skip` moves on; come back to skipped steps at the end.\n")
    w("**Budget.** You have 50 minutes (`bash /app/timer.sh` shows what is left) and about 180 commands; the")
    w("commands usually run out first. Every command's output stays in your context, so combine edits, keep")
    w("output short (`| tail -n 30`, `sed -n` ranges), and never print a large file twice.\n")

    # -- 3. other files
    w("## Other files\n")
    extra = ["`step.sh`: the step runner (see [Order of work](#order-of-work)).",
             "`restart.sh`: restarts the cluster cleanly and waits until it is healthy (see [Working safely](#working-safely))."]
    if has_reg:
        extra.append("`checks/regression.sh`: one script holding every Roles and Channels rule; prints only what is broken, then `REGRESSION: <n> broken`.")
    for c in built:
        cm = COMP[c]
        if c in new or has_ab_checks:
            k = cm["key"]
            if cm["all"]:
                extra.append("`%s/checks/%s`: %s in one command." % (k, cm["all"][0], cm["all"][2]))
            extra.append("`%s/checks/lib.sh`, `%s/checks/%s`: used by the checks; do not run them." % (k, k, cm["core"]))
    for e in extra:
        w("- " + e)
    w("")
    if specs or has_ab_specs:
        w("Each spec file has **Uses** (functions from earlier specs: call them, do not rewrite them), **Adds**")
        w("(what it introduces, with the names to use) and numbered **Rules** (its check tests each by number).\n")
    if specs:
        w("**Do not read the source of the checks** (`*_core.py`, `*_core.sh`, `lib.sh`, `regression.sh`): the")
        w("spec files are the rules. Build from the specs and use the checks' output to find what is wrong.\n")
    else:
        w("**The checks are the specification.** Each one sends real requests and says what it expected. To")
        w("see exactly what a rule needs, read the check's source (`*_core.py`) and the route in the task description.\n")

    # -- 4. scope
    w("## Scope\n")
    left = [COMP[c]["name"] for c in "ABCDE" if c not in built]
    if left:
        w("Do NOT start on these in this run, except where a rule here needs a piece of them: %s.\n" % "; ".join(left))
    if not ab:
        w("Roles and Channels are not in this `/spec`. Build only the basics the steps need, as the task")
        w("description describes them (no roles, invitations, search, pins or read state unless a rule asks):")
        w("register/login/me (tokens valid on every node); `POST /api/workspaces` (creates `#general`, creator is")
        w("`owner`), `GET /api/workspaces`, `GET /api/workspaces/{slug}`, its `members`; creating and joining")
        w("channels, `GET /api/channels/{id}/members`; posting (`201 {message}`) and listing messages (newest")
        w("first); the WebSocket (`subscribe` → `subscribed` with `head_event_id`, `resume` → missed events then")
        w("`resumed`, durable frames with a dense per-channel `event_id` on every node).\n")
    shared = []
    if "D" in built and "E" in built:
        shared.append("IRC bridging while a node is dead is built and checked with the IRC gateway (IrcUi rule 25).")
    if "E" in built:
        shared.append("The web UI also calls threads, reactions, invitations, member roles and channel settings;"
                      " build any that no other step provides, as the task description describes them.")
    if "C" in built and "E" in built:
        shared.append("Reactions and `/invite` are built in Features; the UI only calls them.")
    if "D" in new and len(new) > 1:
        shared.append("Cluster comes before Features and IRC/UI: its restarts, Redis fallback and fan-out shape"
                      " every other part and are much harder to add at the end.")
    for s in shared:
        w("- " + s)
    if shared:
        w("")

    # -- 5. architecture (same for every run)
    w("## How to build the cluster\n")
    w("- **One shared store**: one SQLite file in `/app/data` (WAL, 5 s busy timeout) used by every process;")
    w("  no state only in one process's memory.")
    w("- **Events**: each durable event gets its `event_id` from a per-channel counter row, in the same")
    w("  transaction (`BEGIN IMMEDIATE`) that stores the change and the event row (kept for `resume`).")
    w("- **Fan-out**: publish committed events on Redis (`127.0.0.1:6379`) and also poll the events table every")
    w("  50-100 ms, so delivery survives a dead Redis. Send each `event_id` to a socket at most once.")
    w("- **`/app/start.sh`** stays in the foreground and supervises one process each for `redis-server`, the")
    w("  HTTP nodes on `8000`, `8001`, `8002`%s, restarting any that dies within a second or two. It writes" %
      (" and the IRC gateway on `6667`" if "E" in built else ""))
    w("  its PID to `/tmp/start.pid` and stops its children on `SIGTERM`. List Python deps in `/app/requirements.txt`.\n")

    # -- 6. baseline (same for every run)
    w("## Do not break the baseline\n")
    w("When you submit: (1) `GET /api/health` on `:8000` returns `200` `{\"status\": \"ok\", \"node_id\": 0}`;")
    w("(2) ports `8000`, `8001`, `8002` report `node_id` `0`, `1`, `2`; (3) a WebSocket with an invalid token")
    w("is rejected; (4) if one HTTP node is killed, the other two keep answering `/api/health` with `200`.\n")

    # -- 7. working safely (same for every run)
    w("## Working safely\n")
    w("- Before a scripted edit, back the file up (`cp /app/x.py /tmp/x.py.bak`); afterwards run `python3 -m")
    w("  py_compile` and `wc -l`, and restore the backup if it shrank or broke. Prefer several small files.")
    w("- **Never stop processes by name** (`pkill -f`, `killall`, `pgrep -f`, parsing `ps`): that can kill you.")
    w("  Use a saved PID or the PID that owns a port (`ss -ltnp`).")
    w("- **Restart only with `bash /spec/restart.sh`**, as a command of its own, after every code change. It stops")
    w("  `/app/start.sh` and anything still holding the ports, starts it again and waits until all three nodes")
    w("  answer. Do not start `/app/start.sh` by hand a second time: the old nodes keep the ports (`address")
    w("  already in use` in `/tmp`) and every check then tests your old code.")
    killers = sorted("%s/checks/%s" % (COMP[c]["key"], s[4]) for c in built for s in COMP[c]["steps"]
                     if s[4] in KILLERS and (c in new or has_ab_checks))
    if has_reg:
        killers.insert(0, "checks/regression.sh")
    if killers:
        w("- %s kill a process **on purpose** and wait for `/app/start.sh` to bring it back." % ", ".join(code(k) for k in killers))
    w("- Run every check as a command of its own; never chain a restart, a `sleep` and a check (commands are")
    w("  cut off after 30 s). A check starts `/app/start.sh` if the cluster is down and uses random names, so")
    w("  you can rerun it freely. Each ends with a summary line such as `RESULT: 12 passed, 0 failed`.\n")

    # -- 8. before you submit
    w("## Before you submit\n")
    w("Run `bash /spec/step.sh --status`: every step must be `PASSED`, apart from steps you skipped and could")
    w("not fix in the time left.\n")
    w("Submit (`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`) only when, in output you saw after your last code")
    w("change, every check in the table ends with `RESULT: <rules> passed, 0 failed`%s%s, and the baseline holds." %
      (", `regression.sh` with `REGRESSION: 0 broken`" if has_reg else "",
       "".join(", `%s` with `RESULT: %d passed, 0 failed`" % (COMP[c]["all"][0], COMP[c]["all"][1])
               for c in built if COMP[c]["all"] and (c in new or has_ab_checks))))
    w("If `bash /app/timer.sh` shows under 4 minutes and something still fails, stop adding features: restart")
    w("the cluster, confirm the baseline, and submit. These rules are necessary, not sufficient.")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- folder assembly
def build(run, save=False):
    cfg = RUNS[run]
    ab = cfg["ab"]
    if ab in ("regression", "specs+regression") and not os.path.exists(REGRESSION):
        sys.exit("spec-src/regression/regression.sh does not exist yet: write the regression suite first")
    os.makedirs(DEST, exist_ok=True)
    for name in os.listdir(DEST):  # empty the folder, keep .gitkeep
        if name == ".gitkeep":
            continue
        p = os.path.join(DEST, name)
        shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    comps = list(cfg["comps"])
    for c in (["A", "B"] if ab else []) + comps:
        k = COMP[c]["key"]
        src, dst = os.path.join(SRC, k), os.path.join(DEST, k)
        with_checks = c in comps or ab == "full"
        with_specs = (c in comps and not cfg.get("checks_only")) or (c in "AB" and ab in ("specs+regression", "full"))
        if not (with_checks or with_specs):
            continue
        os.makedirs(dst)
        if with_specs:
            for f in os.listdir(src):
                if f.endswith(".md"):
                    text = open(os.path.join(src, f)).read()
                    open(os.path.join(dst, f), "w").write(text)
        if with_checks:
            shutil.copytree(os.path.join(src, "checks"), os.path.join(dst, "checks"),
                            ignore=shutil.ignore_patterns("__pycache__"))
            lib = os.path.join(dst, "checks", "lib.sh")
            s = open(lib).read()
            s2 = re.sub(r"^FEEDBACK=\w+", "FEEDBACK=" + cfg["mode"], s, count=1, flags=re.M)
            assert s2.count("FEEDBACK=" + cfg["mode"]) >= 1
            open(lib, "w").write(s2)
    if ab in ("regression", "specs+regression"):
        # The suite is one script holding every A and B rule: the A and B rule code is embedded in it.
        out = os.path.join(DEST, "checks")
        os.makedirs(out)
        suite = open(REGRESSION).read()
        for mark, src in (("@@ROLES_RULES@@", "roles/checks/check_roles_core.sh"),
                          ("@@CHANNELS_RULES@@", "channels/checks/check_channels_core.py"),
                          ("@@BASELINE_RULES@@", "roles/checks/check_roles_4.sh")):
            body = open(os.path.join(SRC, src)).read().rstrip("\n")
            assert "__REGRESSION_" not in body and suite.count(mark) == 1
            suite = suite.replace(mark, body)
        open(os.path.join(out, "regression.sh"), "w").write(suite)
    open(os.path.join(DEST, "README.md"), "w").write(readme(run))
    open(os.path.join(DEST, "step.sh"), "w").write(step_sh(run))
    open(os.path.join(DEST, "restart.sh"), "w").write(RESTART_SH)
    for dirpath, _, files in os.walk(DEST):
        for f in files:
            if f.endswith(".sh"):
                os.chmod(os.path.join(dirpath, f), 0o755)
    if save:
        out = os.path.join(COPIES, run)
        if os.path.exists(out):
            shutil.rmtree(out)
        shutil.copytree(DEST, out, ignore=shutil.ignore_patterns(".gitkeep"))
    print("built run %s into %s%s" % (run, os.path.relpath(DEST, ROOT), " (saved to spec-copies/%s)" % run if save else ""))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1 or args[0] not in RUNS:
        sys.exit(__doc__)
    build(args[0], save="--save" in sys.argv)
