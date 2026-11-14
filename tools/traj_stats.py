"""Per graded repeat: how the run ended, how often the agent ran checks, skips, and the last
step.sh --status it saw. Usage: python3 tools/traj_stats.py  (reads graded-runs.tsv)"""
import json, re, glob, csv, sys
STATUS_ROW = re.compile(r"^\s*\d+\.\s+\S+\s+(?:PASSED|SKIPPED|FAILED|TODO|NOT RUN|not passed)", re.M)
RUN_CHECK = re.compile(r"\bbash\s+\S*(?:step\.sh(?![^\n;&|]*--(?:status|skip))|check_\w+\.sh|regression\.sh)")
rows = [r for r in csv.DictReader(open("graded-runs.tsv"), delimiter="\t") if "retry" not in r["note"]]
out = {}
for r in rows:
    t = glob.glob(f"tasks/slack-clone-course/jobs/{r['job_folder']}/*/agent/mini-swe-agent.trajectory.json")[0]
    d = json.load(open(t)); msgs = d["messages"]
    n_cmd = n_check = n_skip = 0; last_status = ""; pending_status = False
    for m in msgs:
        c = m["content"] if isinstance(m["content"], str) else json.dumps(m["content"])
        if m["role"] == "assistant":
            for cmd in re.findall(r"```mswea_bash_command\n(.*?)```", c, re.S):
                n_cmd += 1
                n_check += len(RUN_CHECK.findall(cmd))
                n_skip += len(re.findall(r"step\.sh\s+--skip", cmd))
                pending_status = "step.sh --status" in cmd
        elif m["role"] == "user" and (pending_status or STATUS_ROW.search(c)):
            last_status = c; pending_status = False
    st = re.findall(r"^\s*\|?.*?(?:passed|failed|skipped|not run|PASS|FAIL|SKIP).*$", last_status, re.M)
    out[f"{r['run']}_r{r['repeat']}"] = dict(exit=d["info"].get("exit_status"), cmds=n_cmd, checks=n_check,
        skips=n_skip, cost=float(r["cost_usd"]), last_status=last_status[-6000:])
json.dump(out, open("rescore/traj_stats.json", "w"), indent=1)
for k, v in out.items():
    print(f"{k:7} {v['exit']:15} cmds={v['cmds']:3} checks={v['checks']:3} skips={v['skips']} ${v['cost']:.2f}")
