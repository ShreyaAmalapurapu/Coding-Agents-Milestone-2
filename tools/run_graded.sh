#!/usr/bin/env bash
# Runs the 22 graded runs of the milestone 2 run plan, one at a time, in order (1.1 ... 5.3, 2 repeats).
#
#   nohup caffeinate -i bash tools/run_graded.sh >> graded-runs.out 2>&1 &
#   touch graded-runs.stop     # stop cleanly after the repeat that is running
#
# For each repeat: verifies the frozen sources (spec-copies/FROZEN.sha256), builds that run's /spec,
# checks it is identical to spec-copies/<run>/, runs the agent, and appends a line to graded-runs.tsv
# (run, repeat, job folder, cost, start, end, note). Repeats already in graded-runs.tsv are skipped,
# so the script can be restarted after an interruption. It stops if two repeats in a row cost almost
# nothing (the agent quit at once, usually because the OpenAI credits ran out). At the end it rebuilds
# Run 5.3, so milestone-2-specs/ holds the submission again.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
JOBS="$ROOT/tasks/slack-clone-course/jobs"
SPEC="$ROOT/tasks/slack-clone-course/course-staff-files/final_task/environment/milestone-specs/milestone-2-specs"
LOG="$ROOT/graded-runs.tsv"
RUNS="1.1 1.2 1.3 2.1 2.2 2.3 3 4 5.1 5.2 5.3"
REPEATS="1 2"
CHEAP=0.10      # a repeat that cost less than this did no real work
[ -f "$LOG" ] || printf 'run\trepeat\tjob_folder\tcost_usd\tstarted\tfinished\tnote\n' >"$LOG"

say() { echo "[$(date '+%F %T')] $*"; }
cheap_in_a_row=0
for run in $RUNS; do
  for rep in $REPEATS; do
    if awk -F'\t' -v r="$run" -v p="$rep" '$1==r && $2==p && $7!~/retry/ {f=1} END {exit !f}' "$LOG"; then
      say "run $run repeat $rep already done, skipping"; continue
    fi
    # `touch graded-runs.stop` asks the driver to stop before the next repeat (the current one finishes)
    if [ -f "$ROOT/graded-runs.stop" ]; then
      rm -f "$ROOT/graded-runs.stop"
      python3 tools/build_spec.py 5.3 >/dev/null
      say "STOPPED on request (graded-runs.stop) before run $run repeat $rep; milestone-2-specs/ rebuilt for Run 5.3"
      exit 0
    fi
    shasum -a 256 -c --quiet spec-copies/FROZEN.sha256 || { say "STOP: frozen sources changed"; exit 1; }
    python3 tools/build_spec.py "$run" >/dev/null || { say "STOP: build of $run failed"; exit 1; }
    diff -rq --exclude=.gitkeep "$SPEC" "spec-copies/$run" >/dev/null || { say "STOP: built /spec for $run differs from spec-copies/$run"; exit 1; }
    for attempt in 1 2; do
      before=$(ls "$JOBS" 2>/dev/null | sort)
      started=$(date '+%F %T')
      say "starting run $run repeat $rep (attempt $attempt)"
      (cd tasks/slack-clone-course/student-files && ./run_student_openai.sh 2) >"$ROOT/graded-runs.current.log" 2>&1
      finished=$(date '+%F %T')
      job=$(comm -13 <(echo "$before") <(ls "$JOBS" 2>/dev/null | sort) | tail -n 1)
      cost=$(python3 -c 'import json,sys; print("%.4f" % (json.load(open(sys.argv[1])).get("stats", {}).get("cost_usd") or 0))' \
             "$JOBS/$job/result.json" 2>/dev/null || echo 0)
      note=""
      if [ -z "$job" ]; then note="no job folder"; cost=0; fi
      if python3 -c "import sys; sys.exit(0 if float('$cost') < $CHEAP else 1)"; then
        note="${note:+$note; }quit early"
        [ "$attempt" = 1 ] && note="$note; retry"
      fi
      printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$run" "$rep" "${job:--}" "$cost" "$started" "$finished" "$note" >>"$LOG"
      say "run $run repeat $rep: job ${job:--}, cost \$$cost${note:+ ($note)}"
      case "$note" in
        *"quit early"*) cheap_in_a_row=$((cheap_in_a_row + 1)) ;;
        *) cheap_in_a_row=0; break ;;
      esac
      if [ "$cheap_in_a_row" -ge 2 ]; then
        say "STOP: two repeats in a row quit early (out of OpenAI credits?). See graded-runs.current.log"
        python3 tools/build_spec.py 5.3 >/dev/null
        exit 1
      fi
    done
  done
done
python3 tools/build_spec.py 5.3 >/dev/null
say "ALL DONE. milestone-2-specs/ rebuilt for Run 5.3. Total cost: \$$(awk -F'\t' 'NR>1 {s+=$4} END {printf "%.2f", s}' "$LOG")"
