"""
Human-in-the-loop escalation & handoff (REPORT.md section 5 / brief 3.6).

Control-transfer model:
  - A `Session` wraps the single live browser/page object used by both
    automation and, during handoff, the human.
  - `who_controls` is the source of truth for who is allowed to act on the
    session right now: "automation" | "human". Every automation action
    checks this before acting; a human console (mocked here as a CLI
    prompt / could be a real co-browsing UI) checks it before allowing input.
  - `InterventionRequest` is the payload raised when automation cannot
    safely proceed: it carries the goal/capability, the current step, a
    reason, and a path to a screenshot/HTML snapshot of the exact moment —
    enough for a human to orient without replaying the whole run themselves.
  - `escalate()` pauses automation (sets who_controls="human"), raises the
    request, and BLOCKS until a human action is recorded (mocked here via
    an operator function passed in — in production this would be a queue
    + real operator UI). `resume()` records what the human did, hands
    control back, and lets replay continue from the next step or terminate.

Scope note (per brief 4/8): the operator UI itself is mocked as a function
call. What's real is the pause/cede/resume mechanism and the fact that the
SAME session/page object is what the human acts on, not a fresh one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional


Controller = Literal["automation", "human"]


@dataclass
class InterventionRequest:
    run_id: str
    capability_name: str
    step_id: str
    reason: str
    context_snapshot_path: Optional[str] = None
    current_state_summary: str = ""


@dataclass
class HumanAction:
    """What the human did during their control window, for the record."""
    description: str
    resulting_state_summary: str = ""
    resolved: bool = True  # False if the human also gave up (hard failure)


@dataclass
class Session:
    """
    Wraps the single live page/session object shared by automation and the
    human during a handoff. `page` is intentionally typed loosely here
    (Any) so this module has no hard Playwright dependency — the replay/
    discovery engines pass their live Playwright Page in.
    """
    page: Any
    who_controls: Controller = "automation"
    handoff_log: list[HumanAction] = field(default_factory=list)

    def require_automation_control(self) -> None:
        if self.who_controls != "automation":
            raise RuntimeError(
                "Automation attempted to act while control is held by a human. "
                "This should never happen — it indicates a control-transfer bug."
            )


OperatorFn = Callable[[InterventionRequest, Session], HumanAction]


class EscalationManager:
    def __init__(self, operator_fn: OperatorFn, evidence_recorder=None):
        """
        operator_fn: called synchronously with (request, session) and must
        return a HumanAction once the human has acted on the LIVE session
        (session.page). In this project it's a stand-in for a real operator
        console + queue; swapping it for a real one does not change the
        contract with the rest of the system.
        """
        self.operator_fn = operator_fn
        self.evidence = evidence_recorder

    def escalate(self, session: Session, request: InterventionRequest) -> HumanAction:
        session.who_controls = "human"
        if self.evidence:
            self.evidence.log(
                "escalation_raised",
                step_id=request.step_id,
                reason=request.reason,
                context_snapshot_path=request.context_snapshot_path,
            )
        action = self.operator_fn(request, session)
        session.handoff_log.append(action)
        session.who_controls = "automation"
        if self.evidence:
            self.evidence.log(
                "escalation_resolved",
                step_id=request.step_id,
                human_action=action.description,
                resolved=action.resolved,
            )
        return action


def cli_operator_fn(request: InterventionRequest, session: Session) -> HumanAction:
    """
    Minimal real (not mocked-away) operator surface for this project: a
    blocking CLI prompt. It genuinely pauses the automated run and waits for
    a person, and the person is told they can act on the live page/session
    directly (e.g. via the same Playwright page object, exposed here for a
    real interactive Python shell) before typing what they did.

    A production operator console would replace this function body with a
    real UI notification + a co-browsing view of `session.page` — the
    contract (block, return a HumanAction, hand control back) is unchanged.
    """
    print("\n=== INTERVENTION REQUIRED ===")
    print(f"Capability : {request.capability_name}")
    print(f"Stuck at   : {request.step_id}")
    print(f"Reason     : {request.reason}")
    print(f"Snapshot   : {request.context_snapshot_path}")
    print("The live session is now under human control (session.page).")
    desc = input("Describe what you did to resolve this (or 'give up'): ").strip()
    resolved = desc.lower() != "give up"
    return HumanAction(description=desc or "(no description given)", resolved=resolved)
