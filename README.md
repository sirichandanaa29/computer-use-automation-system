# Computer-Use Automation System

A record-once / replay-many system that lets an AI agent operate a legacy,
API-less back-office UI: an LLM figures out a flow once (discovery), the
run is captured as a typed, reviewable **artifact**, and the artifact
replays afterward **without the model in the loop** (deterministic replay),
with an explicit human-escalation path when replay gets stuck.

See `/REPORT.md` for the design write-up (architecture, schema, error
handling, multi-tenant story, safety model, and what was cut).

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install chromium   # downloads a browser binary — needs
                                          # normal internet access; this step
                                          # could not be run inside the
                                          # sandboxed environment this repo
                                          # was authored in (browser-binary
                                          # CDNs are not on its allowlist)
export ANTHROPIC_API_KEY=sk-ant-...      # only needed for `discover`
```

## Run without live services

The target app is fully local (Flask, no external dependencies), and the
whole suite of unit tests runs with **no browser at all** — they exercise
the replay engine, guardrails, and escalation logic against a fake page
(`tests/fake_page.py`):

```bash
python3 -m pytest tests/ -v
```

## Demo path

**1. Start the mock target application** (a deliberately legacy-feeling
"CoreBank Teller Console" — server-rendered HTML, table layout, no test
IDs, with injectable runtime errors):

```bash
python3 target_app/app.py
# serves on http://localhost:5000
```

**2. Run a real discovery pass** (this is the part that needs your own
`ANTHROPIC_API_KEY` and a real installed browser — see Section 5 of the
brief: "the discovery run has to be real"):

```bash
python3 -m cua_system.cli discover \
  --goal "look up member 12345, open a new youth_savings sub-account with a \$50 initial deposit, and reach the confirmation screen" \
  --entry-url "http://localhost:5000/" \
  --out artifacts/open_subaccount_discovered.json \
  --artifact-id open_subaccount_discovered \
  --name "Open New Sub-Account (discovered)"
```

This drives a real Chromium page via the model's decisions (observe via
accessibility tree → decide next action → act), and writes:
- the resulting artifact to `--out`
- evidence (structured JSONL log of every model decision and action) to
  `evidence/discovery_<run_id>/`

A hand-authored reference artifact for the same flow — shaped exactly like
what that discovery run should produce — is checked in at
`artifacts/open_subaccount_v1.json`, so replay can be demoed immediately
without requiring your own API key first.

**3. Replay the artifact deterministically** (no model involved):

```bash
# success path
python3 -m cua_system.cli replay \
  --artifact artifacts/open_subaccount_v1.json \
  --param member_id=12345 --param account_type=youth_savings --param deposit=50 \
  --preapprove-irreversible

# business outcome: member not found (not a crash — a legitimate answer)
python3 -m cua_system.cli replay \
  --artifact artifacts/open_subaccount_v1.json \
  --param member_id=99999 --param account_type=youth_savings --param deposit=50

# business outcome: validation error (deposit below the $25 minimum)
python3 -m cua_system.cli replay \
  --artifact artifacts/open_subaccount_v1.json \
  --param member_id=12345 --param account_type=youth_savings --param deposit=5

# escalation: step 7 is IRREVERSIBLE and not pre-approved above -> without
# --preapprove-irreversible, replay pauses and hands the live session to a
# human via a blocking CLI prompt (the mocked operator surface)
python3 -m cua_system.cli replay \
  --artifact artifacts/open_subaccount_v1.json \
  --param member_id=12345 --param account_type=youth_savings --param deposit=50
```

Each replay writes structured evidence (`evidence/replay_<run_id>/`):
`run_log.jsonl` (every step, decision, and outcome), `result.json` (the
final structured outcome), and a failure HTML snapshot if replay got stuck.

## Project layout

```
target_app/          mock legacy target application (Flask)
cua_system/
  schema.py           Artifact / Step / locator / outcome data model
  agent.py            LLM-driven discovery loop (produces an Artifact)
  replay.py           deterministic replay engine (production execution path)
  guardrails.py       allowlist enforcement + risk classification
  escalation.py       human-in-the-loop handoff / control-transfer model
  evidence.py         structured logging + redaction
  cli.py              discover / replay entrypoints
tests/                unit tests against a fake Playwright page (no browser needed)
artifacts/            saved capability artifacts
evidence/             run logs (created at runtime)
```
