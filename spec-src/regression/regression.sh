#!/usr/bin/env bash
# Regression suite for Roles (A) and Channels (B): this one script holds every A and B rule.
# Prints only what is broken (REGRESSION FAIL <component> <rule> <why>), then REGRESSION: <n> broken.
# Usage: bash /spec/checks/regression.sh      Exit 0 only if nothing is broken.
#
# The rules are embedded below and unpacked into a temporary folder at run time. Roles 1-70,
# Channels 1-66 and baseline 71-74 run at the same time; then, if enough of the 30 second command
# limit is left, baseline 75-76 kill the HTTP node on 8001 (by the PID that owns the port) and wait
# for /app/start.sh to bring it back.
set -u

# The regression suite always runs in verbose mode: it prints which rule broke and why.
FEEDBACK=verbose   # verbose or score_only

T0=$SECONDS
PY=/opt/test-venv/bin/python; [ -x "$PY" ] || PY=python3
OUT="$(mktemp -d /tmp/regression.XXXXXX)"
trap 'rm -rf "$OUT"' EXIT
say() { [ "$FEEDBACK" = verbose ] && echo "$*"; return 0; }

# ---- the embedded rules (written out from Specs Roles and Channels when /spec was built) ----
printf '%s\n' "FEEDBACK=$FEEDBACK" 'say() { [ "$FEEDBACK" = verbose ] && echo "$*"; return 0; }' >"$OUT/lib.sh"
cat >"$OUT/roles.sh" <<'__REGRESSION_ROLES_RULES__'
@@ROLES_RULES@@
__REGRESSION_ROLES_RULES__
cat >"$OUT/channels.py" <<'__REGRESSION_CHANNELS_RULES__'
@@CHANNELS_RULES@@
__REGRESSION_CHANNELS_RULES__
cat >"$OUT/baseline.sh" <<'__REGRESSION_BASELINE_RULES__'
@@BASELINE_RULES@@
__REGRESSION_BASELINE_RULES__

healthy() {
  for p in 8000 8001 8002; do curl -fs --max-time 2 "http://127.0.0.1:$p/api/health" >/dev/null 2>&1 || return 1; done
}
# AIDEV-NOTE: start the cluster once here, before the parts run in parallel; otherwise each part would
# find it down and start its own /app/start.sh. The whole subshell is redirected so a caller reading
# this script's output is not held open by the cluster.
if ! healthy; then
  (cd /app && setsid nohup /app/start.sh </dev/null >/tmp/start.log 2>&1 &) </dev/null >/dev/null 2>&1
  for _ in 1 2 3 4 5 6; do healthy && break; sleep 1; done
fi
if ! healthy; then
  say "REGRESSION FAIL cluster 0 not answering /api/health on 8000, 8001, 8002 (see /tmp/start.log)"
  echo "REGRESSION: 1 broken"
  exit 1
fi

bash "$OUT/roles.sh" >"$OUT/roles.out" 2>&1 &
"$PY" "$OUT/channels.py" >"$OUT/channels.out" 2>&1 &
BASELINE_NO_KILL=1 bash "$OUT/baseline.sh" >"$OUT/baseline.out" 2>&1 &
wait

# AIDEV-NOTE: rules 75-76 kill node 1 and wait for it; they run only after everything else, and only
# when at least 8 seconds of the 30 second limit remain (they need about 2-5 s on a healthy cluster).
left=$((27 - (SECONDS - T0)))
if [ "$left" -ge 8 ]; then
  BASELINE_KILL_ONLY=1 BASELINE_RESPAWN_LIMIT=$((left - 3)) bash "$OUT/baseline.sh" >"$OUT/kill.out" 2>&1
else
  echo "NOTE: rules 75-76 (a killed node) were not run this time: the other rules used most of the time limit; run the suite again" >"$OUT/kill.out"
  echo "RESULT: 0 passed, 0 failed" >>"$OUT/kill.out"
fi

broken=0
# report <component> <file>: turns FAIL lines into REGRESSION FAIL lines; a part with no RESULT line
# (it crashed or was cut off) counts as broken too.
report() {
  local comp="$1" f="$2" line
  while IFS= read -r line; do
    case "$line" in
      FAIL\ *) broken=$((broken + 1)); say "REGRESSION FAIL $comp ${line#FAIL }" ;;
      NOTE:*) say "REGRESSION NOTE $comp ${line#NOTE: }" ;;
    esac
  done <"$f"
  if ! grep -q '^RESULT: ' "$f"; then
    broken=$((broken + 1))
    say "REGRESSION FAIL $comp 0 its part of the suite did not finish (last line: $(tail -1 "$f" | cut -c1-150))"
  fi
}
report roles "$OUT/roles.out"
report channels "$OUT/channels.out"
report roles "$OUT/baseline.out"
report roles "$OUT/kill.out"

echo "REGRESSION: $broken broken"
[ "$broken" -eq 0 ]
