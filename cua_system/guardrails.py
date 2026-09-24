"""
Safety & policy guardrails (REPORT.md section 6).

Two independent mechanisms, deliberately kept separate:
  1. AllowlistPolicy  - WHERE the agent/replay is permitted to act at all
     (domains/routes, action types). A hard boundary, checked before every
     navigation and action, for both discovery and replay.
  2. RiskClassifier    - WHAT KIND of action a step is (safe / reversible /
     irreversible), used to decide whether it may proceed automatically or
     must be confirmed / escalated. This is orthogonal to the allowlist:
     something can be in-allowlist and still be irreversible.

Both are policy-as-data (plain config), not embedded in step logic, so a
reviewer can audit the policy without reading code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from .schema import ActionType, RiskLevel, Step


class PolicyViolation(Exception):
    """Raised when an action would step outside the allowlist."""


@dataclass
class AllowlistPolicy:
    allowed_domains: list[str]
    allowed_action_types: set[ActionType] = field(default_factory=lambda: {
        ActionType.NAVIGATE, ActionType.CLICK, ActionType.FILL,
        ActionType.SELECT, ActionType.WAIT_FOR, ActionType.EXTRACT,
        ActionType.ASSERT_CHECKPOINT, ActionType.DISMISS_IF_PRESENT,
    })
    # routes containing any of these substrings are blocked outright,
    # regardless of domain (e.g. admin/config surfaces out of scope for a
    # given capability)
    blocked_path_substrings: list[str] = field(default_factory=list)

    def check_url(self, url: str) -> None:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if not any(host == d or host.endswith("." + d) for d in self.allowed_domains):
            raise PolicyViolation(
                f"URL '{url}' is outside the allowed domain list {self.allowed_domains}"
            )
        for blocked in self.blocked_path_substrings:
            if blocked in parsed.path:
                raise PolicyViolation(
                    f"Path '{parsed.path}' matches blocked pattern '{blocked}'"
                )

    def check_action(self, action: ActionType) -> None:
        if action not in self.allowed_action_types:
            raise PolicyViolation(f"Action type '{action}' is not in the allowlist")


class RiskDecision:
    PROCEED = "proceed"
    REQUIRE_CONFIRMATION = "require_confirmation"
    BLOCK = "block"


@dataclass
class RiskClassifier:
    """
    Conservative-by-default handling of the risky class, per the brief:
    SAFE and REVERSIBLE steps proceed automatically; IRREVERSIBLE steps
    require an explicit confirmation flag to have been set for this run
    (e.g. a human pre-approved this capability's irreversible step during
    artifact review), otherwise they are escalated rather than blocked
    outright — blocking silently would just make the capability useless,
    escalating preserves both safety and progress.
    """
    irreversible_preapproved: bool = False

    def decide(self, step: Step) -> str:
        if step.risk == RiskLevel.SAFE:
            return RiskDecision.PROCEED
        if step.risk == RiskLevel.REVERSIBLE:
            return RiskDecision.PROCEED
        # IRREVERSIBLE
        if self.irreversible_preapproved:
            return RiskDecision.PROCEED
        return RiskDecision.REQUIRE_CONFIRMATION


def default_target_app_policy() -> AllowlistPolicy:
    return AllowlistPolicy(
        allowed_domains=["localhost", "127.0.0.1"],
        blocked_path_substrings=["/admin", "/config"],
    )
