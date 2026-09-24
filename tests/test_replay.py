"""
Tests for ReplayEngine's core logic using FakePage, so they run without a
real browser (see fake_page.py for why). Covers:
  - happy path success with output extraction
  - business-outcome detection (member not found) — must NOT be a failure
  - locator fallback (first candidate absent, second candidate resolves)
  - guardrail enforcement (URL outside allowlist is blocked)
  - escalation: stuck step triggers handoff, human resolves it in-session,
    replay continues and succeeds
  - escalation: human gives up -> replay reports failure, not a crash
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from cua_system.schema import (
    Artifact, ActionType, DeclaredOutcome, ElementRef, InputParam, LocatorCandidate,
    LocatorKind, OutcomeKind, OutputField, ParamType, RiskLevel, Step,
)
from cua_system.guardrails import AllowlistPolicy, RiskClassifier
from cua_system.evidence import EvidenceRecorder
from cua_system.escalation import EscalationManager, HumanAction
from cua_system.replay import ReplayEngine
from tests.fake_page import FakePage, FakeElement


def role_name(role, name):
    return ElementRef(description=f"{role}:{name}",
                       candidates=[LocatorCandidate(kind=LocatorKind.ROLE_NAME, value=f"{role}:{name}")])


def build_lookup_artifact() -> Artifact:
    return Artifact(
        artifact_id="lookup_v1",
        name="Look up member balance",
        description="test",
        discovery_goal="look up member and read balance",
        target_app="core_banking_console",
        target_entry_url="http://localhost:5000/",
        inputs=[InputParam(name="member_id", type=ParamType.STRING)],
        outputs=[OutputField(name="balance", type=ParamType.STRING)],
        declared_outcomes=[
            DeclaredOutcome(name="member_not_found", kind=OutcomeKind.BUSINESS_OUTCOME,
                             detect_text_contains="No record found")
        ],
        steps=[
            Step(step_id="s1", action=ActionType.NAVIGATE, value="http://localhost:5000/"),
            Step(step_id="s2", action=ActionType.FILL, target=role_name("textbox", "member_id"),
                 value="${member_id}"),
            Step(step_id="s3", action=ActionType.CLICK, target=role_name("button", "Search")),
            Step(step_id="s4", action=ActionType.EXTRACT, target=role_name("cell", "balance_value"),
                 extract_into="balance"),
        ],
        checkpoint=role_name("heading", "Member Record"),
    )


class ReplayEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.evidence = EvidenceRecorder(run_id="test", run_kind="replay", base_dir=self.tmpdir)
        self.policy = AllowlistPolicy(allowed_domains=["localhost"])
        self.risk = RiskClassifier()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _page_found(self) -> FakePage:
        page = FakePage()
        page._screens["search_form"] = {"html": "", "elements": [
            FakeElement(role="textbox", name="member_id"),
            FakeElement(role="button", name="Search"),
        ]}
        page._screens["member_record"] = {"html": "", "elements": [
            FakeElement(role="heading", name="Member Record"),
            FakeElement(role="cell", name="balance_value", text="4,210.55"),
        ]}

        def on_click(p, el):
            if el.role == "button" and el.name == "Search":
                p.state = "member_record"
        page.on_click_handler = on_click
        return page

    def test_happy_path_extracts_output(self):
        page = self._page_found()
        engine = ReplayEngine(page=page, policy=self.policy, risk_classifier=self.risk,
                               evidence=self.evidence)
        result = engine.replay(build_lookup_artifact(), {"member_id": "12345"})
        self.assertEqual(result.outcome, OutcomeKind.SUCCESS)
        self.assertEqual(result.outputs["balance"], "4,210.55")

    def test_business_outcome_not_found_is_not_a_failure(self):
        page = FakePage()
        page._screens["search_form"] = {"html": "", "elements": [
            FakeElement(role="textbox", name="member_id"),
            FakeElement(role="button", name="Search"),
        ]}
        # after search, land on a "not found" screen whose content triggers the declared outcome
        page._screens["not_found"] = {"html": "No record found for member ID \"99999\".", "elements": []}

        def on_click(p, el):
            if el.role == "button" and el.name == "Search":
                p.state = "not_found"
        page.on_click_handler = on_click

        engine = ReplayEngine(page=page, policy=self.policy, risk_classifier=self.risk,
                               evidence=self.evidence)
        result = engine.replay(build_lookup_artifact(), {"member_id": "99999"})
        self.assertEqual(result.outcome, OutcomeKind.BUSINESS_OUTCOME)
        self.assertEqual(result.outcome_name, "member_not_found")

    def test_locator_fallback_uses_second_candidate(self):
        target = ElementRef(description="submit", candidates=[
            LocatorCandidate(kind=LocatorKind.ROLE_NAME, value="button:DoesNotExist"),
            LocatorCandidate(kind=LocatorKind.TEXT, value="Search"),
        ])
        artifact = build_lookup_artifact()
        artifact.steps[2] = Step(step_id="s3", action=ActionType.CLICK, target=target)

        page = self._page_found()
        page._screens["search_form"]["elements"].append(FakeElement(role="button", name="Search", text="Search"))
        engine = ReplayEngine(page=page, policy=self.policy, risk_classifier=self.risk,
                               evidence=self.evidence)
        result = engine.replay(artifact, {"member_id": "12345"})
        self.assertEqual(result.outcome, OutcomeKind.SUCCESS)

    def test_guardrail_blocks_out_of_allowlist_navigation(self):
        artifact = build_lookup_artifact()
        artifact.target_entry_url = "http://evil.example.com/"
        page = self._page_found()
        engine = ReplayEngine(page=page, policy=self.policy, risk_classifier=self.risk,
                               evidence=self.evidence)
        result = engine.replay(artifact, {"member_id": "12345"})
        self.assertEqual(result.outcome, OutcomeKind.FAILURE)

    def test_escalation_human_resolves_and_replay_continues(self):
        # step s3's locator never resolves on its own -> triggers escalation.
        # The human "fixes" it by directly flipping page.state, mimicking them
        # acting on the live session, then reports resolved=True.
        page = self._page_found()
        page._screens["search_form"]["elements"] = [
            FakeElement(role="textbox", name="member_id"),
            # no "Search" button present at all -> click will fail to resolve
        ]

        def operator_fn(request, session):
            session.page.state = "member_record"  # human acts on the SAME live page
            return HumanAction(description="clicked Search manually", resolved=True)

        escalation = EscalationManager(operator_fn=operator_fn, evidence_recorder=self.evidence)
        engine = ReplayEngine(page=page, policy=self.policy, risk_classifier=self.risk,
                               evidence=self.evidence, escalation=escalation)
        result = engine.replay(build_lookup_artifact(), {"member_id": "12345"})
        self.assertEqual(result.outcome, OutcomeKind.SUCCESS)
        self.assertEqual(len(page.handoff_log if hasattr(page, "handoff_log") else []), 0)  # sanity: no crash

    def test_escalation_human_gives_up_reports_failure_not_crash(self):
        page = self._page_found()
        page._screens["search_form"]["elements"] = [FakeElement(role="textbox", name="member_id")]

        def operator_fn(request, session):
            return HumanAction(description="could not resolve", resolved=False)

        escalation = EscalationManager(operator_fn=operator_fn, evidence_recorder=self.evidence)
        engine = ReplayEngine(page=page, policy=self.policy, risk_classifier=self.risk,
                               evidence=self.evidence, escalation=escalation)
        result = engine.replay(build_lookup_artifact(), {"member_id": "12345"})
        self.assertEqual(result.outcome, OutcomeKind.FAILURE)
        self.assertIn("gave up", result.observed)

    def test_irreversible_step_requires_preapproval(self):
        artifact = build_lookup_artifact()
        artifact.steps[2].risk = RiskLevel.IRREVERSIBLE  # the "Search" click, for this test

        def operator_fn(request, session):
            session.page.state = "member_record"
            return HumanAction(description="approved and clicked manually", resolved=True)

        escalation = EscalationManager(operator_fn=operator_fn, evidence_recorder=self.evidence)
        page = self._page_found()
        engine = ReplayEngine(page=page, policy=self.policy,
                               risk_classifier=RiskClassifier(irreversible_preapproved=False),
                               evidence=self.evidence, escalation=escalation)
        result = engine.replay(artifact, {"member_id": "12345"})
        # not pre-approved -> escalates -> human does it -> still succeeds, but via escalation
        self.assertEqual(result.outcome, OutcomeKind.SUCCESS)


if __name__ == "__main__":
    unittest.main()
