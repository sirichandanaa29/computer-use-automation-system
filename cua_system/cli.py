"""
CLI entrypoint. See README.md for the exact commands.

  python3 -m cua_system.cli discover --goal "..." --entry-url "..." --out artifacts/x.json
  python3 -m cua_system.cli replay --artifact artifacts/x.json --param member_id=12345
  python3 -m cua_system.cli replay --artifact artifacts/x.json --param member_id=99999
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid

from playwright.sync_api import sync_playwright

from .schema import Artifact
from .guardrails import default_target_app_policy, RiskClassifier
from .evidence import EvidenceRecorder
from .escalation import EscalationManager, cli_operator_fn
from .replay import ReplayEngine
from .agent import DiscoveryAgent


def cmd_discover(args: argparse.Namespace) -> None:
    run_id = uuid.uuid4().hex[:8]
    evidence = EvidenceRecorder(run_id=run_id, run_kind="discovery")
    policy = default_target_app_policy()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        page = browser.new_page()
        agent = DiscoveryAgent(page=page, policy=policy, evidence=evidence, model=args.model)
        try:
            artifact = agent.discover(
                goal=args.goal, entry_url=args.entry_url,
                artifact_id=args.artifact_id, name=args.name,
            )
        finally:
            browser.close()

    with open(args.out, "w") as f:
        json.dump(artifact.model_dump(), f, indent=2)
    print(f"Discovery succeeded. Artifact written to {args.out}")
    print(f"Evidence: evidence/discovery_{run_id}/")


def cmd_replay(args: argparse.Namespace) -> None:
    with open(args.artifact) as f:
        artifact = Artifact.model_validate(json.load(f))

    params: dict[str, str] = {}
    for kv in args.param or []:
        k, _, v = kv.partition("=")
        params[k] = v

    run_id = uuid.uuid4().hex[:8]
    evidence = EvidenceRecorder(run_id=run_id, run_kind="replay")
    policy = default_target_app_policy()
    risk = RiskClassifier(irreversible_preapproved=args.preapprove_irreversible)
    escalation = EscalationManager(operator_fn=cli_operator_fn, evidence_recorder=evidence)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        page = browser.new_page()
        engine = ReplayEngine(page=page, policy=policy, risk_classifier=risk,
                               evidence=evidence, escalation=escalation)
        result = engine.replay(artifact, params)
        browser.close()

    print(f"Outcome: {result.outcome.value}")
    if result.outcome_name:
        print(f"Business outcome: {result.outcome_name} — {result.message}")
    if result.outputs:
        print(f"Outputs: {result.outputs}")
    if result.outcome.value == "failure":
        print(f"Failed at step {result.failed_step_id}")
        print(f"Expected: {result.expected}")
        print(f"Observed: {result.observed}")
    print(f"Evidence: evidence/replay_{run_id}/")
    sys.exit(0 if result.outcome.value in ("success", "business_outcome") else 1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Computer-use automation system")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="Run a real LLM-driven discovery pass")
    d.add_argument("--goal", required=True)
    d.add_argument("--entry-url", required=True)
    d.add_argument("--out", required=True)
    d.add_argument("--artifact-id", default="discovered_capability")
    d.add_argument("--name", default="Discovered Capability")
    d.add_argument("--model", default="claude-sonnet-4-6")
    d.add_argument("--headless", action="store_true", default=True)
    d.set_defaults(func=cmd_discover)

    r = sub.add_parser("replay", help="Deterministically replay a saved artifact")
    r.add_argument("--artifact", required=True)
    r.add_argument("--param", action="append", help="key=value, repeatable")
    r.add_argument("--preapprove-irreversible", action="store_true", default=False)
    r.add_argument("--headless", action="store_true", default=True)
    r.set_defaults(func=cmd_replay)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
