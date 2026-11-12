#!/usr/bin/env bash
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
"Cluster.1|cluster/cluster-1-redis-fanout.md|cluster/checks/check_cluster_1.sh|result"
"Cluster.2|cluster/cluster-2-node-failure.md|cluster/checks/check_cluster_2.sh|result"
"Cluster.3|cluster/cluster-3-redis-failure.md|cluster/checks/check_cluster_3.sh|result"
"Cluster.4|cluster/cluster-4-performance.md|cluster/checks/check_cluster_4.sh|result"
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
