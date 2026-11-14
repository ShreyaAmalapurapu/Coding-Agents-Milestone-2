#!/usr/bin/env bash
# Reruns my checks on the agent's final code (artifacts/app/) of every graded repeat, so every row of
# the report is scored the same way, after the run, on a fresh container.
#
#   bash tools/rescore.sh [run_repeat ...]     e.g. bash tools/rescore.sh 5.3_r1   (default: all 22)
#
# Each repeat gets its own container (task image), with a copy of artifacts/app as /app and the
# frozen Run 5.3 /spec (verbose checks for all five components) as /spec. It scores the components
# of that run with the per-step checks the agent ran (each has the same 22 s budget as in the run;
# the *_all.sh checks cannot fit every rule in it):  C = check_features_1..5,  D = check_cluster_1..4,
# E = check_irc_ui_1..7, and in Runs 5.x also A (check_roles_1..4) and B (check_channels_1..4), plus in
# 5.1/5.2 the regression suite the agent got (spec-copies/<run>/checks/regression.sh).
# The cluster is restarted (/spec/restart.sh) before each check, so a check that kills a node
# cannot affect the next one. Output: rescore/<run>_r<rep>.log, last lines "SCORE <comp> p f".
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
IMAGE=${IMAGE:-final_task__ngck2hk__env-main:latest}
JOBS="$ROOT/tasks/slack-clone-course/jobs"
OUT="$ROOT/rescore"; mkdir -p "$OUT"
PAR=${PAR:-1}   # one at a time, like the run: parallel containers slow the timed rules

inner() { cat <<'IN'
set -u
cp -R /app_src/. /app/ && cd /app
[ -s requirements.txt ] && pip install -q -r requirements.txt >/tmp/pip.log 2>&1
chmod +x /app/start.sh 2>/dev/null
run() {  # run <comp> <check...>: restart, run each check, add up its RESULT line
  local comp=$1 p=0 f=0 c line; shift
  for c in "$@"; do
    bash /spec/restart.sh >/dev/null 2>&1
    echo "=== $comp $c"
    line=$(timeout 150 bash "$c" 2>&1 | tee /dev/stderr | grep -E '^RESULT: ' | tail -1)
    if [ -z "$line" ]; then
      echo "NO RESULT from $c"
      line="RESULT: 0 passed, 1 failed"   # a check that crashed: counted as 1 failed, noted in the log
    fi
    p=$((p + $(echo "$line" | sed -E 's/RESULT: ([0-9]+) passed.*/\1/')))
    f=$((f + $(echo "$line" | sed -E 's/.* ([0-9]+) failed.*/\1/')))
  done
  echo "SCORE $comp $p $f"
}
case " $COMPS " in *" C "*) run C /spec/features/checks/check_features_{1,2,3,4,5}.sh ;; esac
case " $COMPS " in *" D "*) run D /spec/cluster/checks/check_cluster_{1,2,3,4}.sh ;; esac
case " $COMPS " in *" E "*) run E /spec/irc-ui/checks/check_irc_ui_{1,2,3,4,5,6,7}.sh ;; esac
case " $COMPS " in *" A "*) run A /spec/roles/checks/check_roles_{1,2,3,4}.sh ;; esac
case " $COMPS " in *" B "*) run B /spec/channels/checks/check_channels_{1,2,3,4}.sh ;; esac
if [ -f /reg/regression.sh ]; then
  bash /spec/restart.sh >/dev/null 2>&1
  echo "=== REG"; timeout 150 bash /reg/regression.sh 2>&1 | tee /tmp/reg.txt
  echo "SCORE REG $(grep -oE 'REGRESSION: [0-9]+' /tmp/reg.txt | grep -oE '[0-9]+' || echo NA)"
fi
IN
}

one() {  # one <run> <rep> <job>
  local run=$1 rep=$2 job=$3 comps="C D E" app reg=()
  case $run in 1.1|2.1) comps="C";; 1.2|2.2) comps="D";; 1.3|2.3) comps="E";; 5.*) comps="C D E A B";; esac
  app=$(ls -d "$JOBS/$job"/*/artifacts/app | head -1)
  [ -f "spec-copies/$run/checks/regression.sh" ] && reg=(-v "$ROOT/spec-copies/$run/checks:/reg:ro")
  docker run --rm -e COMPS="$comps" -v "$app:/app_src:ro" -v "$ROOT/spec-copies/5.3:/spec:ro" "${reg[@]}" \
    --entrypoint bash "$IMAGE" -c "$(inner)" >"$OUT/${run}_r${rep}.log" 2>&1
  echo "done $run r$rep: $(grep '^SCORE' "$OUT/${run}_r${rep}.log" | tr '\n' ' ')"
}

want=" $* "
while read -r run rep job; do
  [ "$want" = "  " ] || case "$want" in *" ${run}_r${rep} "*) ;; *) continue ;; esac
  while [ "$(jobs -rp | wc -l)" -ge "$PAR" ]; do sleep 2; done
  one "$run" "$rep" "$job" &
done < <(awk -F'\t' 'NR>1 && $7!~/retry/ {print $1, $2, $3}' graded-runs.tsv)
wait
echo "ALL RESCORED"
