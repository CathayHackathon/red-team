# aegis-redteam

A closed-loop adversarial testing harness for AegisOps's self-improving agent.
It doesn't just find failures — it turns them into guardrail patches, then
re-tests to prove the platform actually got safer. That loop *is* the
self-improvement mechanism this hackathon project is about.

## Pipeline

```
seed attacks --> attacker (mutate/escalate) --> target (agent under test)
                                                     |
                                              judge scores response
                                                     |
                                          confirmed findings (JSONL)
                                                     |
                                   feedback.py: derive guardrail patches
                                                     |
                              apply to target's policy --> re-run seeds
                                                     |
                                    report.py: baseline vs. post-patch
```

- **Target adapter** (`target.py`): anything implementing `respond(history) -> str`.
  Ships with `MockAegisTarget`, a toy agent with realistic, deliberately
  partial guardrails (see below), and `HTTPTarget` to point at a real
  AegisOps endpoint once one exists.
- **Attack taxonomy** (`attacks.py`, `seeds/seeds.yaml`): prompt injection,
  jailbreak, goal hijacking, reward hacking, unsafe self-modification,
  data exfiltration, resource abuse, deceptive-alignment probing, privilege
  escalation. Each category has a severity weight.
- **Attacker** (`attacker.py`): PAIR/TAP-style loop — rewrites/escalates a
  seed based on the target's last response, up to `max_turns`.
- **Judge** (`judge.py`): a separate pass (never the attacker grading itself)
  that decides whether the target actually complied, and how severely.
- **Feedback** (`feedback.py`): confirmed high-severity findings become
  concrete, human-reviewable guardrail patch proposals, tagged by category
  and backed by the evidence (seed IDs) that justified them.
- **Report** (`report.py`): a single self-contained HTML file — success
  rate by category/severity, top findings, and (in the demo) a side-by-side
  baseline vs. post-patch comparison.

## Why the mock target is "deliberately imperfect," not fake

`MockAegisTarget` only guards the two categories a naive filter would catch
for free (direct secret requests, direct "ignore instructions"-style
injection). Every other category — self-modification, privilege escalation,
resource abuse, deceptive-alignment probes, goal hijacking, reward hacking —
has **zero** coverage until a red-team campaign finds it and a patch is
applied for that specific category. That's the realistic failure mode this
system is built to catch: safety you didn't test for is safety you don't
have. Swap in `AnthropicBackend` (set `ANTHROPIC_API_KEY`) for a real LLM
attacker/judge/target instead of the offline heuristics.

## Run it

```bash
pip install -r requirements.txt

# Full self-improvement-loop demo: baseline -> patch -> re-run -> compare
python3 demo.py
open runs/demo_report.html   # or just open the file in a browser

# Or drive the pieces separately:
python3 -m aegis_redteam.cli run --run-id baseline
python3 -m aegis_redteam.cli report --run-id baseline --out runs/report.html
python3 -m aegis_redteam.cli feedback --run-id baseline
```

Set `ANTHROPIC_API_KEY` to switch the attacker/judge/target calls (where
applicable) from the offline mock backend to real Claude calls — nothing
else changes.

## Logging (Cloud Logging, no extra client library)

Every track the harness records — each attack turn, each judge verdict,
each finding, each patch proposal, each patch application, job start/finish
— is emitted as one JSON line to stdout via `gcp_logging.py`. That's the
entire integration: Cloud Run automatically parses stdout/stderr JSON lines
with a top-level `severity` and `message` key as structured Cloud Logging
entries, so there's no `google-cloud-logging` client, no extra IAM role
beyond what the Cloud Run service account already has, and no network call
from the job itself.

A confirmed finding's log severity is derived from its attack severity
(0-4), not left at a flat INFO — severity 4 findings log as `CRITICAL`,
3 as `ERROR`, 2 as `WARNING` — so a genuinely dangerous bypass is visible in
Logs Explorer without a custom filter, and a log-based metric on
`jsonPayload.event="finding_confirmed" AND jsonPayload.attack_severity>=4`
gives you Cloud Monitoring alerting on critical findings for free.

Every line also carries `run_id` (and `seed_id` where relevant), and when
running as a Cloud Run Job, `cloud_run_execution` / `cloud_run_task_index`
from the environment — so every line from one triggered campaign can be
isolated in Logs Explorer with `jsonPayload.run_id="..."`.

This is deliberately separate from the findings store (`store.py`): the
store is the queryable "current results" table the HTML report reads;
Cloud Logging is the immutable, timestamped audit trail of everything that
happened, across every run, kept as long as your log retention/sink policy
says — including who/what triggered each run, once the trigger service is
built (log the caller and request body there too).

## Wiring in the real AegisOps agent

1. Expose a POST endpoint accepting `{"messages": [...]}`, returning `{"reply": "..."}`.
2. `target = HTTPTarget(url="https://.../agent")` in place of `MockAegisTarget`.
3. Implement `HTTPTarget.apply_patch` against AegisOps's own config-update
   API so `feedback.py`'s proposals can be applied automatically (behind a
   human-approval gate — see "Governance" below).

## Governance notes for the real system

- Never let the harness run against a target with real destructive tool
  access — sandbox the target during red-team campaigns.
- Patch proposals should default to **human review before auto-apply**;
  auto-apply is a later-stage optimization once the harness has a track
  record.
- Version every campaign against the target's config/prompt version so you
  get true regression testing across self-improvement cycles, not just a
  point-in-time score.
- Rate-limit and sandbox the attacker loop itself — it's an agent with a
  goal ("break the target"), so give it the same operational scrutiny as
  any other autonomous agent in the platform.
