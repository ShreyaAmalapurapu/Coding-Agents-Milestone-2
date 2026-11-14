# Milestone 2 report

- Name: Shreya Amalapurapu
- UNI: SA4342

All results are from my own checks.

**How I got the numbers.** The agent's own check output during a run isn't a fair final score: steps
it skipped never pass, and sometimes it broke a step after passing it. So once all 22 repeats were
done, I reran my checks on each repeat's final code (`artifacts/app/`) with `tools/rescore.sh`.
- Each repeat gets a fresh copy of the task container, and they run one at a time.
- Every repeat uses the Run 5.3 checks in `verbose` mode, so the `score_only` runs aren't graded
  differently.
- The cluster is restarted before each check.
- In 5.1 and 5.2 I also ran the regression suite those agents had, which gives the
  `A and B: N broken` line.

My first try used the `check_*_all.sh` checks. They can't fit every rule into the 22 second time limit
I built in for the agent's 30 second command limit, and they made Run 1.1 look like 13 of 56 passing.
So I switched to the per-step checks, the same ones the agent ran. Rule counts: A 76, B 66, C 56, D 21,
E 53. The logs are in `rescore/`.

"Ran checks" counts every command where the agent ran a check (`step.sh`, a `check_*.sh` or
`regression.sh`). Running `step.sh --status` or `--skip` doesn't count. "Skipped" means it used
`step.sh --skip`, which is only allowed after a step fails 6 times. I got these from the transcripts
with `tools/traj_stats.py`.

## Runs

One row per repeat of each run (22 in total). "Run" is the run in the run plan in README Setup.

| Run | Component | Repeat | Job folder | Check results | Cost | Ran checks (times) | Submitted with failures? | Problems |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1.1 | C | 1 | `2026-11-12__17-16-37` | C: 53 passed, 3 failed | $1.12 | 45 | Yes, skipped Features.4 and 5 | Rule 39 (`/archive`) fails, and 55-56 run out of time. It couldn't tell which rule was failing, so it hit the 6-run limit twice. |
| 1.1 | C | 2 | `2026-11-12__17-45-04` | C: 53 passed, 3 failed | $1.81 | 69 | Yes, skipped Features.4 and 5 | Same 3 rules as repeat 1. 3 command timeouts. |
| 1.2 | D | 1 | `2026-11-12__20-06-30` | D: 20 passed, 1 failed | $0.15 | 12 | Yes, skipped Cluster.2 | Rule 12: after a node restarts, its new post doesn't reach 8002. It gave up and submitted at only $0.15. |
| 1.2 | D | 2 | `2026-11-12__20-17-58` | D: 21 passed, 0 failed | $0.21 | 9 | No | None |
| 1.3 | E | 1 | `2026-11-12__20-24-56` | E: 29 passed, 24 failed | $1.44 | 50 | Yes, skipped 6 of 7 steps | Only IrcUi.4 passed. Failures in edits, the IRC bridge and all the browser steps. 3 command timeouts. |
| 1.3 | E | 2 | `2026-11-12__20-45-54` | E: 33 passed, 20 failed | $1.37 | 52 | Yes, skipped 5 of 7 steps | IRC bridge (22-25), browser login (31-37) and UI flows (46-53) fail. 4 command timeouts. |
| 2.1 | C | 1 | `2026-11-12__21-09-17` | C: 56 passed, 0 failed | $0.55 | 19 | No | None |
| 2.1 | C | 2 | `2026-11-12__21-18-23` | C: 56 passed, 0 failed | $1.45 | 35 | No | Its first versions were slow, so some rules ran out of time, but it fixed that. |
| 2.2 | D | 1 | `2026-11-12__21-37-17` | D: 21 passed, 0 failed | $0.42 | 23 | No | Cluster.1 and Cluster.2 took 9 and 8 tries. |
| 2.2 | D | 2 | `2026-11-12__21-52-50` | D: 21 passed, 0 failed | $0.34 | 18 | No | Tried to skip a step too early (refused), then fixed it. |
| 2.3 | E | 1 | `2026-11-12__22-07-51` | E: 53 passed, 0 failed | $2.38 | 65 | No | IrcUi.6 took 31 tries. |
| 2.3 | E | 2 | `2026-11-12__22-40-48` | E: 52 passed, 1 failed | $0.99 | 46 | Yes, but it didn't know: all its steps passed | Rule 50 fails in my rerun (a close button isn't visible). Either it broke later or the check is flaky. |
| 3 | C, D, E | 1 | `2026-11-12__23-00-35` | C: 56 passed, 0 failed; D: 21 passed, 0 failed; E: 46 passed, 7 failed | $2.08 | 74 | Yes, but it didn't know: all 16 steps passed | IrcUi.5 (browser login, 31-37) passed during the run, but later UI changes broke it. |
| 3 | C, D, E | 2 | `2026-11-12__23-26-26` | C: 56 passed, 0 failed; D: 21 passed, 0 failed; E: 49 passed, 4 failed | $1.89 | 74 | Yes, but it didn't know: all 16 steps passed | Same thing: IrcUi.5 rules 34-37 broke after passing. |
| 4 | C, D, E | 1 | `2026-11-12__23-50-33` | C: 49 passed, 7 failed; D: 21 passed, 0 failed; E: 33 passed, 20 failed | $2.87 | 87 | Yes, skipped Features.5, IrcUi.6, IrcUi.7 | Also broke Features.1 (rules 3-8) and IrcUi.4-5 after they had passed. |
| 4 | C, D, E | 2 | `2026-11-13__00-24-12` | C: 54 passed, 2 failed; D: 15 passed, 6 failed; E: 51 passed, 2 failed | $2.93 | 70 | Yes, skipped Features.5 and IrcUi.7 | All the Cluster steps passed during the run, but 6 D rules fail at the end. |
| 5.1 | A to E | 1 | `2026-11-13__01-00-05` | A and B: 10 broken; C: 53 passed, 3 failed; D: 21 passed, 0 failed; E: 36 passed, 17 failed | $5.01 | 126 | Didn't submit, hit the $5 limit | Never used `step.sh`. Started C to E with A and B still 9 broken, then ran `check_irc_ui_5.sh` 56 times. |
| 5.1 | A to E | 2 | `2026-11-13__01-38-42` | A and B: 47 broken; C: 50 passed, 6 failed; D: 21 passed, 0 failed; E: 48 passed, 5 failed | $2.40 | 67 | Yes, its last regression run said 46 broken; skipped IrcUi.5-7 | Skipped the A and B step at 69 broken, then didn't rerun the regression suite for about 190 messages. The server stops answering A's requests. |
| 5.2 | A to E | 1 | `2026-11-13__02-09-08` | A and B: 3 broken (registration fails, so nothing else in A or B can run); C: 0 passed, 56 failed; D: 2 passed, 19 failed; E: 5 passed, 48 failed | $5.01 | 92 | Didn't submit, hit the $5 limit | It had passed all 24 steps, then an edit moving the PATCH routes deleted the POST handler (every POST returns 501). It ran out of money before fixing it. |
| 5.2 | A to E | 2 | `2026-11-13__02-39-55` | A and B: 10 broken; C: 18 passed, 38 failed; D: 6 passed, 15 failed; E: 35 passed, 18 failed | $1.38 | 33 | Yes, never checked C or D | Left the A and B steps at 21 broken, never ran a C or D check, and submitted at $1.38. The IRC server wasn't running. |
| 5.3 | A to E | 1 | `2026-11-13__02-58-56` | A: 76 passed, 0 failed; B: 66 passed, 0 failed; C: 56 passed, 0 failed; D: 21 passed, 0 failed; E: 41 passed, 12 failed | $4.18 | 79 | Yes, skipped Channels.3 and IrcUi.6 | IrcUi.6 and browser login fail. Channels.3 passes in my rerun, so it probably failed during the run because of timing. |
| 5.3 | A to E | 2 | `2026-11-13__03-27-49` | A: 76 passed, 0 failed; B: 66 passed, 0 failed; C: 56 passed, 0 failed; D: 21 passed, 0 failed; E: 53 passed, 0 failed | $3.06 | 72 | No | None |

The 22 repeats cost $43.05 in total.

## Experiments

### 1) Feedback

> Do you find that adding detailed output to the checks helps the agent?

#### Runs compared and why

I compared Runs 1.1, 1.2 and 1.3 (`score_only`) with Runs 2.1, 2.2 and 2.3 (`verbose`). Each pair builds
the same single component with the same spec, README and checks. The only change is the `FEEDBACK=` line
in `lib.sh`, so any difference should come from the feedback.

#### Supporting results

| Component | `score_only` (1.x) r1, r2 | `verbose` (2.x) r1, r2 | Steps skipped (1.x vs 2.x) | Check runs (1.x vs 2.x) | Cost (1.x vs 2.x) |
| --- | --- | --- | --- | --- | --- |
| C (56 rules) | 53, 53 | 56, 56 | 4 vs 0 | 45, 69 vs 19, 35 | $1.12, $1.81 vs $0.55, $1.45 |
| D (21 rules) | 20, 21 | 21, 21 | 1 vs 0 | 12, 9 vs 23, 18 | $0.15, $0.21 vs $0.42, $0.34 |
| E (53 rules) | 29, 33 | 53, 52 | 11 vs 0 | 50, 52 vs 65, 46 | $1.44, $1.37 vs $2.38, $0.99 |
| Total | 209 / 260 (80%) | 259 / 260 (99.6%) | 16 vs 0 | | $6.10 vs $6.13 |

#### Answer

Yes, the detailed output helped a lot. With `verbose` checks the agent passed 259 of 260 rules and never
skipped a step. With `score_only` it passed 209 of 260 and skipped 16 steps. The total cost was about the
same ($6.10 vs $6.13), so the extra detail didn't cost more; the money just went to different things.

The difference depended on the component.
- **D:** almost no difference (1 rule), probably because the D rules are mostly about status codes and
  nodes coming back, so the agent could guess what was wrong.
- **E:** a huge difference (20-24 rules). E has a browser UI and an IRC bridge, and "4 passed, 5 failed"
  tells you nothing about which of those is broken.

With `score_only` the agent mostly reran the same failing check until it hit the 6-run limit and skipped.

#### Consistency across repeats

Pretty consistent. Both `score_only` C repeats failed the exact same 3 rules and skipped the same 2 steps,
and both `score_only` E repeats skipped 5 or 6 of the 7 steps. All six `verbose` repeats finished every
step. The only exception is D: 1.2 repeat 2 passed everything even without details, so for a small, simple
component the count can be enough.

#### Other evidence

- In 1.3 repeat 1 the agent never got past the first step, IrcUi.1. It kept changing things and rerunning
  without knowing which of the 9 rules was failing.
- The `verbose` runs also had hard steps (IrcUi.6 took 31 tries in 2.3 repeat 1), but they kept going and
  eventually passed. So I think the details, not the budget, made the difference.
- Two `score_only` agents (1.1 repeat 1 and 1.2 repeat 1) submitted with most of their $5 left after
  skipping steps. Once a step was skipped, there was nothing telling them to go back to it.

### 2) Tests as the spec

> Can the checks alone replace the written rules?

#### Runs compared and why

I compared Run 3 with Run 4. Both build C, D and E together with `verbose` checks and the same README. The
only difference is that Run 3 also gets the written spec files, while Run 4 has to figure out the rules
from the checks alone.

#### Supporting results

| Component | Run 3 (specs + checks) r1, r2 | Run 4 (checks only) r1, r2 |
| --- | --- | --- |
| C (56 rules) | 56, 56 | 49, 54 |
| D (21 rules) | 21, 21 | 21, 15 |
| E (53 rules) | 46, 49 | 33, 51 |
| Total (of 130) | 123, 126 (95.8%) | 103, 120 (85.8%) |
| Steps skipped (of 16) | 0, 0 | 3, 2 |
| Check runs | 74, 74 | 87, 70 |
| Cost | $2.08, $1.89 | $2.87, $2.93 |

#### Answer

Not completely. With only the checks, the agent passed about 10% fewer rules (85.8% vs 95.8%), skipped
5 steps (Run 3 skipped none), and spent about 46% more money ($5.80 vs $3.97 for both repeats).

The checks do get most of the way there: Run 4 still built the whole cluster in repeat 1 and almost all of
E in repeat 2. What goes missing is stuff a check can't say clearly:
- **Longer behaviours.** Features.5 (groups and mentions) was skipped in both Run 4 repeats.
- **UI flows** in IrcUi.6 and IrcUi.7.
- **Why a rule exists.** Run 4 broke rules it had already passed (Features.1 in repeat 1, Cluster in
  repeat 2), and I think part of the reason is that it had no written rule to look back at.

#### Consistency across repeats

The overall direction is consistent: both Run 4 repeats scored below both Run 3 repeats, and both skipped
Features.5. But where Run 4 lost points changed. Repeat 1 did badly on E (33 of 53), and repeat 2 did
badly on D (15 of 21). So I'm confident specs help, but not about which component suffers most without
them.

#### Other evidence

- In Run 4 the agent spent commands reading the check scripts to work out what each rule wants, so steps
  took more tries. Cluster.1 took 4 and 11 tries in Run 4, against 3 and 7 in Run 3.
- In Run 4 repeat 2, every Cluster step passed during the run, but 6 D rules fail at the end. Most of
  Run 4's losses were things it broke later, not things it never understood.

### 3) Scale

> Do you find that a component passes fewer of its checks when the agent builds it together with
> other components, rather than on its own?

#### Runs compared and why

I compared Runs 2.1, 2.2 and 2.3 (each component on its own) with Run 3 (C, D and E together). They all
use `verbose` checks, the same specs and the same README, so the only difference is how much else the
agent has to build in the same run. I didn't use Run 4 here because it also takes away the specs.

#### Supporting results

| Component | On its own (2.x) r1, r2 | Together (Run 3) r1, r2 | Change |
| --- | --- | --- | --- |
| C (56 rules) | 56, 56 | 56, 56 | 0 |
| D (21 rules) | 21, 21 | 21, 21 | 0 |
| E (53 rules) | 53, 52 | 46, 49 | about -5 |
| Cost | $3.35 (r1), $2.78 (r2) for 2.1 + 2.2 + 2.3 | $2.08, $1.89 | |

#### Answer

A little, and only for E, the component built last. C and D passed every rule both ways. E dropped from
53 and 52 on its own to 46 and 49 when built with the others. In both Run 3 repeats the rules that failed
were in IrcUi.5 (browser login). The agent passed that step during the run, but later UI work broke it.
Nothing reruns an earlier step's check after it passes, so the agent never noticed.

I don't think it was a budget problem: Run 3 only used about $2 of its $5 and skipped nothing. It's more
that a bigger run means more later changes that can break earlier work.

#### Consistency across repeats

Consistent. Both Run 3 repeats lost points in the same step (IrcUi.5), by similar amounts (7 and 4 rules),
and both kept C and D perfect.

#### Other evidence

- The same thing shows up in Runs 4 and 5.x: the parts built at the end (E, and Features.5) are the ones
  that score worse in my rerun than they did during the run.
- Building everything together was actually cheaper than three separate runs ($2.08 and $1.89 vs $3.35
  and $2.78), because the basics like login, workspaces and channels only had to be built once.

### 4) Regression

> a) Do you find that building components C, D, and E breaks features from A and B that were
> already working?

> b) Does it make a difference whether the agent has Specs A and B or only their regression
> tests?

> c) Does it make a difference whether the A and B checks are given as a regression suite or as
> plain checks?

#### Runs compared and why

- **a)** Runs 5.1, 5.2 and 5.3. In all of them the README tells the agent to build A and B first and get
  their checks passing before starting C, D and E. From the transcript I found where A and B stood when
  the agent started C to E, and I compared that with my rerun on the final code.
- **b)** Run 5.1 (only the A and B regression suite, no specs) against Run 5.2 (Specs A and B plus the
  same regression suite). Everything else is the same.
- **c)** Run 5.2 (A and B checked with the regression suite) against Run 5.3 (Specs A and B with normal
  staged checks, one step each, like C to E).

#### Supporting results

| Run | Repeat | A and B when C to E started | A (76) at the end | B (66) at the end | Regression suite at the end | C + D + E (of 130) | How it ended |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 5.1 (regression only) | 1 | 9 broken (it just moved on) | 75 | 57 | 10 broken | 110 | $5 limit |
| 5.1 (regression only) | 2 | 69 broken (skipped after 6 tries) | 10 (server stops answering) | 34 | 47 broken | 119 | submitted, $2.40 |
| 5.2 (specs + regression) | 1 | 0 broken (all 8 A and B steps passed) | 5 (registration fails) | 0 | 3 broken (all setup) | 7 | $5 limit |
| 5.2 (specs + regression) | 2 | 21 broken (it just moved on) | 75 | 57 | 10 broken | 59 | submitted, $1.38 |
| 5.3 (specs + plain checks) | 1 | 7 of 8 A and B steps passed (Channels.3 skipped) | 76 | 66 | (not given) | 118 | submitted, $4.18 |
| 5.3 (specs + plain checks) | 2 | all 8 A and B steps passed | 76 | 66 | (not given) | 130 | submitted, $3.06 |

#### Answer

**a)** Sometimes, and when it happened it was bad. I expected this question to be about C to E slowly
breaking A and B. Instead, in most of the 5.1 and 5.2 repeats A and B were never working to begin with:
the agent moved on with 9, 21 or 69 rules broken. Only three repeats had A and B fully working before
C to E: 5.2 repeat 1 and both 5.3 repeats.
- **5.2 repeat 1:** building C to E broke A and B. The regression suite went from 0 to 43 broken. Then
  the agent's last edit deleted the POST handler, so even registration failed.
- **Both 5.3 repeats:** A and B stayed perfect (76/76 and 66/66) until the end.

So my answer is yes, it can happen (1 of the 3 cases). The bigger problem was that A and B often never
got finished at all.

**b)** I didn't see a clear difference.
- **Without the specs (5.1):** the agent left A and B at 9 and 69 broken, and they ended at 10 and 47.
- **With the specs (5.2):** it left them at 0 and 21 broken, and they ended at 3 (where nothing could
  even run) and 10.

The one repeat that got A and B fully passing did have the specs, so the specs might help a bit. But one
repeat of each run hit the cost limit, so I can't really tell from two repeats.

**c)** Yes, this made the biggest difference of anything I tested.
- **As normal staged checks (5.3):** A and B were built step by step like everything else. They passed
  (the one skipped step passes in my rerun) and stayed fully working in both repeats. 5.3 also had the
  best C to E scores of all the 5.x runs (118 and 130 of 130).
- **As one regression suite (5.1 and 5.2):** A and B were one giant step that started with 65-100 rules
  broken at once. The agent never really got through that step: it skipped it, moved on, or ran out of
  money. After leaving it, it barely ran the suite again. In 5.1 repeat 2 it didn't rerun the suite from
  message 55 until message 243.

#### Consistency across repeats

- **c):** consistent in every repeat. Both 5.3 repeats finished A and B and kept them working, and all
  four 5.1/5.2 repeats ended with A or B broken.
- **a):** only 3 repeats had A and B working before C to E, so "1 out of 3 broke" isn't strong evidence.
- **b):** the two repeats of each run were very different from each other, with one of each hitting the
  cost limit, so I can't call a difference either way.

#### Other evidence

- The regression suite only prints what's broken. When 65-69 rules are broken at once, there isn't an
  obvious place to start, and after a few tries the agent gave up and moved on to C to E.
- Two of the four 5.1/5.2 agents stopped following the step order. 5.1 repeat 1 never used `step.sh` and
  ran `check_irc_ui_5.sh` 56 times. 5.2 repeat 2 never ran a C or D check before submitting. None of the
  5.3 agents did this.
- Getting here took some fixing on my side. My first 5.3 practice runs failed: the agent ran out of money
  at the old cost limit, skipped the Cluster steps, and got stuck on failing rules for ~30 commands at a
  time. After I raised the cost limit to $5, added the step runner with the 6-run skip limit, and made the
  failure messages clearer, my practice 5.3 run got through all 24 steps. That's why I think how the
  checks are presented matters as much as what they test.
