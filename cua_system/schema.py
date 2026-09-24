"""
Artifact schema: the typed, versioned, agent-invocable capability that a
discovery run produces and that the replay engine executes.

Design intent (see /REPORT.md section 2 for full rationale):
- Steps reference elements via a ranked list of LocatorStrategy candidates,
  not a single selector, so replay can fall back gracefully as the strategy
  degrades (semantic role/name first, then text, then structural fallback).
- Inputs/outputs are typed and declared up front, so a calling agent (or a
  human reviewer) can understand the capability's contract without reading
  the step list.
- Outcomes are classified into three kinds (success / business_outcome /
  failure) at the artifact level, not just at replay-time, so the schema
  itself documents what "no such member" vs. a real crash looks like.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# Locators
# --------------------------------------------------------------------------

class LocatorKind(str, Enum):
    ROLE_NAME = "role_name"        # accessibility role + accessible name (preferred)
    TEXT = "text"                  # visible text content match
    LABEL = "label"                # associated <label> text (form fields)
    CSS_FALLBACK = "css_fallback"  # last-resort CSS selector, flagged as brittle
    TEST_ID = "test_id"            # data-testid, when the app actually has one


class LocatorCandidate(BaseModel):
    """
    One way to find an element. A step carries a ranked LIST of these
    (see ElementRef.candidates) rather than a single selector, because in
    the target environment (no clean DOM, legacy markup) any single
    strategy can break. Replay tries them in order.
    """
    kind: LocatorKind
    value: str
    # human-readable note on why this candidate was chosen / how robust it is
    rationale: str = ""


class ElementRef(BaseModel):
    """A target control, identified by a ranked list of locator candidates."""
    description: str  # e.g. "Member ID search input"
    candidates: list[LocatorCandidate]
    # optional structural scope to disambiguate (e.g. "within #search-form")
    scope_hint: Optional[str] = None


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    WAIT_FOR = "wait_for"          # wait for an element/condition before proceeding
    EXTRACT = "extract"            # read a value out of the page into outputs
    ASSERT_CHECKPOINT = "assert_checkpoint"  # verify expected state was reached
    DISMISS_IF_PRESENT = "dismiss_if_present"  # conditional handling of interstitials


class RiskLevel(str, Enum):
    SAFE = "safe"                  # read-only / trivially reversible (navigate, read)
    REVERSIBLE = "reversible"      # changes state but can be undone (edit a draft field)
    IRREVERSIBLE = "irreversible"  # submits/commits real-world effects (open account,
                                    # move funds, delete) — see guardrails.py


class Step(BaseModel):
    step_id: str
    action: ActionType
    target: Optional[ElementRef] = None
    # For FILL/SELECT: the value to use. May reference an input parameter by
    # name using "${param_name}" — resolved at replay time, never baked in
    # as a literal for anything parameterized.
    value: Optional[str] = None
    # For EXTRACT: which output field this step populates.
    extract_into: Optional[str] = None
    # For ASSERT_CHECKPOINT: the condition description + how it's checked.
    checkpoint_locator: Optional[ElementRef] = None
    expected_text_contains: Optional[str] = None
    risk: RiskLevel = RiskLevel.SAFE
    # human-readable description of *why* this step exists (from the model's
    # reasoning during discovery) — kept for reviewability, decoupled from
    # the raw transcript per the brief's requirement.
    rationale: str = ""
    timeout_ms: int = 5000
    max_retries: int = 1


# --------------------------------------------------------------------------
# Inputs / Outputs
# --------------------------------------------------------------------------

class ParamType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"


class InputParam(BaseModel):
    name: str
    type: ParamType
    required: bool = True
    description: str = ""
    # redaction hint: if true, this value must never be written to logs/
    # evidence in raw form (e.g. an account number, SSN fragment).
    sensitive: bool = False


class OutputField(BaseModel):
    name: str
    type: ParamType
    description: str = ""
    sensitive: bool = False


# --------------------------------------------------------------------------
# Outcome taxonomy (see REPORT.md section 3)
# --------------------------------------------------------------------------

class OutcomeKind(str, Enum):
    SUCCESS = "success"                    # goal achieved, outputs populated
    BUSINESS_OUTCOME = "business_outcome"  # legitimate non-success result the
                                            # caller needs (e.g. "not found")
    RECOVERABLE = "recoverable"            # transient condition, handled inline
                                            # during replay (not a final outcome)
    FAILURE = "failure"                    # hard failure — replay could not
                                            # safely proceed or verify state
    ESCALATED = "escalated"                # handed off to a human operator


class DeclaredOutcome(BaseModel):
    """
    An outcome the artifact author anticipated and gave a name + detection
    rule for, so replay can classify results deterministically rather than
    inferring intent from raw page text at replay time.
    """
    name: str  # e.g. "member_not_found", "duplicate_account_blocked"
    kind: OutcomeKind
    detect_locator: Optional[ElementRef] = None
    detect_text_contains: Optional[str] = None
    description: str = ""


# --------------------------------------------------------------------------
# The artifact itself
# --------------------------------------------------------------------------

class Artifact(BaseModel):
    artifact_id: str
    name: str
    version: int = 1
    description: str

    # Provenance / reviewability
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    discovery_goal: str  # the natural-language goal this was recorded from
    target_app: str      # logical app identifier, e.g. "core_banking_console"
    target_entry_url: str

    # Contract
    inputs: list[InputParam]
    outputs: list[OutputField]
    steps: list[Step]
    checkpoint: ElementRef  # the final assertion that proves success
    declared_outcomes: list[DeclaredOutcome] = Field(default_factory=list)

    # Multi-tenant reuse (see REPORT.md section 4): this artifact was
    # recorded against a base vendor product/version. Tenant-specific
    # overrides live in a separate overlay, never mutate this file.
    vendor_product: Optional[str] = None
    vendor_version: Optional[str] = None

    review_status: Literal["draft", "approved"] = "draft"

    def redacted_copy(self) -> dict[str, Any]:
        """Serialize with sensitive input/output values stripped — this is
        what gets written to logs/evidence, never raw parameter values."""
        d = self.model_dump()
        return d


class TenantOverlay(BaseModel):
    """
    A tenant-specific patch applied on top of a base Artifact at replay
    time. Lets many tenants running the same vendor product reuse one
    artifact instead of re-recording per tenant (REPORT.md section 4).
    """
    overlay_id: str
    base_artifact_id: str
    tenant_id: str
    # step_id -> replacement candidate list (e.g. tenant's branded button text)
    locator_overrides: dict[str, list[LocatorCandidate]] = Field(default_factory=dict)
    entry_url_override: Optional[str] = None
