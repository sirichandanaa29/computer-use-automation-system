"""
Deterministic replay engine (REPORT.md section 3 / brief 3.3).

Given a saved Artifact and input parameters, replays the recorded flow with
NO model in the decision loop. Locator resolution walks each step's ranked
LocatorCandidate list in order (role/name first, falling back toward more
brittle strategies) so the artifact degrades gracefully rather than failing
outright on minor UI variation.

Result contract distinguishes three outcomes, per the brief's explicit
requirement not to conflate them:
  - success            : checkpoint verified, declared outputs extracted
  - business_outcome   : a DeclaredOutcome matched (e.g. "member not found")
                          — this is NOT a failure, it's a real answer
  - failure            : replay could not proceed or verify state; carries
                          step/expected/observed detail for debugging

"Recoverable" conditions (DISMISS_IF_PRESENT steps, brief waits/retries) are
handled inline and never surface as a top-level outcome themselves.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .schema import (
    Artifact, ActionType, ElementRef, LocatorKind, OutcomeKind, Step, TenantOverlay,
)
from .guardrails import AllowlistPolicy, RiskClassifier, RiskDecision, PolicyViolation
from .escalation import EscalationManager, InterventionRequest, Session
from .evidence import EvidenceRecorder


@dataclass
class ReplayResult:
    outcome: OutcomeKind
    outputs: dict[str, Any] = field(default_factory=dict)
    outcome_name: Optional[str] = None      # populated for business_outcome
    failed_step_id: Optional[str] = None    # populated for failure
    expected: Optional[str] = None
    observed: Optional[str] = None
    message: str = ""


class LocatorResolutionError(Exception):
    pass


class ReplayEngine:
    """
    Playwright-backed. Constructed with a live `page` (sync API) so callers
    control browser lifecycle; this class only ever navigates/acts within
    the page it's given.
    """

    def __init__(
        self,
        page: Any,
        policy: AllowlistPolicy,
        risk_classifier: RiskClassifier,
        evidence: EvidenceRecorder,
        escalation: Optional[EscalationManager] = None,
        overlay: Optional[TenantOverlay] = None,
    ):
        self.page = page
        self.policy = policy
        self.risk = risk_classifier
        self.evidence = evidence
        self.escalation = escalation
        self.overlay = overlay
        self.session = Session(page=page)

    # -- locator resolution -------------------------------------------------

    def _candidates_for(self, artifact: Artifact, step: Step) -> list:
        if not step.target:
            return []
        candidates = list(step.target.candidates)
        if self.overlay and step.step_id in self.overlay.locator_overrides:
            # tenant overrides take priority, tried before the base candidates
            candidates = self.overlay.locator_overrides[step.step_id] + candidates
        return candidates

    def _resolve(self, target: ElementRef, candidates: list) -> Any:
        last_err = None
        for cand in candidates:
            try:
                if cand.kind == LocatorKind.ROLE_NAME:
                    role, _, name = cand.value.partition(":")
                    loc = self.page.get_by_role(role, name=re.compile(re.escape(name)))
                elif cand.kind == LocatorKind.LABEL:
                    loc = self.page.get_by_label(cand.value)
                elif cand.kind == LocatorKind.TEXT:
                    loc = self.page.get_by_text(cand.value)
                elif cand.kind == LocatorKind.TEST_ID:
                    loc = self.page.get_by_test_id(cand.value)
                else:  # CSS_FALLBACK
                    loc = self.page.locator(cand.value)
                loc.first.wait_for(state="visible", timeout=2000)
                return loc.first
            except Exception as e:  # noqa: BLE001 - trying next candidate deliberately
                last_err = e
                continue
        raise LocatorResolutionError(
            f"No candidate resolved for '{target.description}': {last_err}"
        )

    # -- outcome detection ----------------------------------------------------

    def _check_declared_outcomes(self, artifact: Artifact) -> Optional[Any]:
        content = self.page.content()
        for outcome in artifact.declared_outcomes:
            if outcome.detect_text_contains and outcome.detect_text_contains in content:
                return outcome
            if outcome.detect_locator:
                try:
                    self._resolve(outcome.detect_locator, outcome.detect_locator.candidates)
                    return outcome
                except LocatorResolutionError:
                    continue
        return None

    # -- main entry point -----------------------------------------------------

    def replay(self, artifact: Artifact, params: dict[str, Any]) -> ReplayResult:
        for inp in artifact.inputs:
            if inp.sensitive:
                self.evidence.mark_sensitive(str(params.get(inp.name, "")))
        self.evidence.save_artifact(artifact.redacted_copy())
        self.evidence.log("replay_start", artifact_id=artifact.artifact_id, params_keys=list(params.keys()))

        outputs: dict[str, Any] = {}
        try:
            self.policy.check_url(artifact.target_entry_url)
        except PolicyViolation as e:
            return self._fail(artifact.steps[0].step_id if artifact.steps else "n/a",
                               "entry URL within allowlist", str(e), str(e))

        # Always navigate to the artifact's recorded entry URL before running
        # any steps. Discovery navigates here BEFORE it starts recording
        # steps (so the model can see the starting page before deciding what
        # to do), which means that first navigation is never itself captured
        # as a Step — replay must therefore always perform it explicitly,
        # rather than relying on the step list to contain a navigate action.
        try:
            self.page.goto(artifact.target_entry_url, timeout=10000)
        except Exception as e:  # noqa: BLE001
            return self._fail("entry", "successful navigation to entry URL", str(e), str(e))

        for step in artifact.steps:
            self.session.require_automation_control()
            try:
                self.policy.check_action(step.action)
            except PolicyViolation as e:
                return self._fail(step.step_id, "action within allowlist", str(e), str(e))

            decision = self.risk.decide(step)
            self.evidence.log("step_start", step_id=step.step_id, action=step.action.value, risk=step.risk.value, decision=decision)

            if decision == RiskDecision.REQUIRE_CONFIRMATION:
                result = self._handle_stuck(
                    artifact, step,
                    reason=f"Step '{step.step_id}' is IRREVERSIBLE and not pre-approved for unattended replay.",
                )
                if result is not None:
                    return result
                continue  # human resolved it and we proceed to next step

            try:
                outcome = self._check_declared_outcomes(artifact)
                if outcome and outcome.kind == OutcomeKind.BUSINESS_OUTCOME:
                    self.evidence.log("business_outcome_detected", name=outcome.name)
                    self.evidence.save_result({"outcome": "business_outcome", "name": outcome.name})
                    return ReplayResult(outcome=OutcomeKind.BUSINESS_OUTCOME, outcome_name=outcome.name,
                                         message=outcome.description)

                self._execute_step(step, params, outputs)
                self.evidence.log("step_ok", step_id=step.step_id)

            except LocatorResolutionError as e:
                result = self._handle_stuck(artifact, step, reason=str(e))
                if result is not None:
                    return result
            except Exception as e:  # noqa: BLE001
                result = self._handle_stuck(artifact, step, reason=f"Unexpected error: {e}")
                if result is not None:
                    return result

        # final checkpoint
        try:
            self._resolve(artifact.checkpoint, artifact.checkpoint.candidates)
        except LocatorResolutionError as e:
            return self._fail(artifact.steps[-1].step_id if artifact.steps else "n/a",
                               f"checkpoint '{artifact.checkpoint.description}' visible",
                               "checkpoint not found", str(e))

        self.evidence.log("replay_success", outputs_keys=list(outputs.keys()))
        self.evidence.save_result({"outcome": "success", "outputs": outputs})
        return ReplayResult(outcome=OutcomeKind.SUCCESS, outputs=outputs)

    # -- step execution ---------------------------------------------------

    def _resolve_value(self, value: Optional[str], params: dict[str, Any]) -> Optional[str]:
        if value is None:
            return None
        if value.startswith("${") and value.endswith("}"):
            return str(params.get(value[2:-1], ""))
        return value

    def _execute_step(self, step: Step, params: dict[str, Any], outputs: dict[str, Any]) -> None:
        if step.action == ActionType.NAVIGATE:
            url = self._resolve_value(step.value, params)
            self.policy.check_url(url)
            self.page.goto(url, timeout=step.timeout_ms)
            return

        if step.action == ActionType.DISMISS_IF_PRESENT:
            try:
                el = self._resolve(step.target, step.target.candidates)
                el.click(timeout=1000)
            except Exception:
                pass  # absence is fine — this is the "if present" case
            return

        if step.action == ActionType.WAIT_FOR:
            self._resolve(step.target, step.target.candidates)
            return

        if step.action == ActionType.ASSERT_CHECKPOINT:
            el = self._resolve(step.checkpoint_locator or step.target,
                                (step.checkpoint_locator or step.target).candidates)
            if step.expected_text_contains:
                text = el.inner_text()
                if step.expected_text_contains not in text:
                    raise LocatorResolutionError(
                        f"Checkpoint text mismatch: expected to contain "
                        f"'{step.expected_text_contains}', observed '{text[:200]}'"
                    )
            return

        el = self._resolve(step.target, step.target.candidates)

        if step.action == ActionType.CLICK:
            el.click(timeout=step.timeout_ms)
        elif step.action == ActionType.FILL:
            el.fill(self._resolve_value(step.value, params) or "", timeout=step.timeout_ms)
        elif step.action == ActionType.SELECT:
            el.select_option(self._resolve_value(step.value, params), timeout=step.timeout_ms)
        elif step.action == ActionType.EXTRACT:
            text = el.inner_text()
            if step.extract_into:
                outputs[step.extract_into] = text.strip()

    # -- stuck handling -----------------------------------------------------

    def _handle_stuck(self, artifact: Artifact, step: Step, reason: str) -> Optional[ReplayResult]:
        snapshot_path = None
        try:
            snapshot_path = self.evidence.save_failure_snapshot(step.step_id, self.page.content())
        except Exception:
            pass
        self.evidence.log("stuck", step_id=step.step_id, reason=reason, snapshot=snapshot_path)

        if not self.escalation:
            return self._fail(step.step_id, "step to complete without intervention", reason, reason, snapshot_path)

        request = InterventionRequest(
            run_id=self.evidence.run_id,
            capability_name=artifact.name,
            step_id=step.step_id,
            reason=reason,
            context_snapshot_path=snapshot_path,
        )
        action = self.escalation.escalate(self.session, request)
        if not action.resolved:
            return self._fail(step.step_id, "human to resolve blocking condition",
                               f"human gave up: {action.description}", reason, snapshot_path)
        return None  # human resolved it in-session; replay continues at next step

    def _fail(self, step_id: str, expected: str, observed: str, message: str,
              snapshot: Optional[str] = None) -> ReplayResult:
        self.evidence.log("replay_failure", step_id=step_id, expected=expected, observed=observed)
        self.evidence.save_result({
            "outcome": "failure", "failed_step_id": step_id,
            "expected": expected, "observed": observed, "message": message,
            "snapshot": snapshot,
        })
        return ReplayResult(outcome=OutcomeKind.FAILURE, failed_step_id=step_id,
                             expected=expected, observed=observed, message=message)
