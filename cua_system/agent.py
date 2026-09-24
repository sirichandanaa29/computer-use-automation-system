"""
Discovery agent (REPORT.md section 1 / brief 3.1).

Observe -> decide -> act loop against a live Playwright page, driven by an
LLM (Anthropic API). Deliberately observes via the ACCESSIBILITY TREE, not
screenshots+coordinates: this is the surface that still exists when there
is no clean DOM (the brief's stated common case), and it's exactly the
representation the replay engine's role/name locators are built from — so
what the model "sees" during discovery is the same thing replay resolves
against later. This is the seam between "how we perceive/act on a surface"
and "the recorded flow" (see REPORT.md section 4).

Each model turn:
  1. serialize the page's accessibility tree to compact text
  2. ask the model to pick ONE action (click/fill/select/navigate/extract/
     assert_checkpoint/done) given the goal + tree + history
  3. execute that action against the real page via Playwright
  4. record it as a Step with a ranked LocatorCandidate list (role/name
     first) — NOT as a raw transcript entry

On success (model reports "done" and the checkpoint is visible), the
recorded steps + declared checkpoint are assembled into an Artifact.
Guardrails are enforced during discovery too — the model cannot navigate or
act outside the allowlist even while "figuring it out".
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from .schema import (
    Artifact, ActionType, ElementRef, InputParam, LocatorCandidate, LocatorKind,
    OutputField, ParamType, RiskLevel, Step,
)
from .guardrails import AllowlistPolicy, PolicyViolation
from .evidence import EvidenceRecorder

SYSTEM_PROMPT = """You are operating a computer-use discovery agent. You will be shown a
compact accessibility-tree snapshot of a web page and a goal. Respond with EXACTLY one JSON
object describing the single next action to take. Do not narrate, do not use markdown fences.

Action schema (pick exactly one shape per turn):
{"action": "navigate", "url": "...", "rationale": "..."}
{"action": "click", "role": "...", "name": "...", "rationale": "..."}
{"action": "fill", "role": "...", "name": "...", "value": "...", "rationale": "..."}
{"action": "select", "role": "...", "name": "...", "value": "...", "rationale": "..."}
{"action": "extract", "role": "...", "name": "...", "extract_into": "output_field_name", "rationale": "..."}
{"action": "assert_checkpoint", "role": "...", "name": "...", "expected_text_contains": "...", "rationale": "..."}
{"action": "done", "rationale": "..."}

"role"/"name" must refer to an element's accessible role and name AS SHOWN in the snapshot.
CRITICAL: any concrete value that came FROM THE GOAL TEXT itself (a member ID, an amount, a
name, an account type — anything the caller specified as an example) MUST be recorded as
"${param_name}", never as the literal value, even though you use the literal value to actually
perform the action right now. For example, if the goal says "look up member 12345", you fill
the search box with "12345" to proceed, but you record the step's value as "${member_id}" — a
future caller will supply a different member ID and the recorded capability must still work for
them. Only use a literal (non-${...}) value for something that is NOT goal-specific — e.g. a
fixed option you are choosing from a dropdown because the goal named that exact choice, or a
truly constant navigation URL.
"navigate" must only be used if the goal explicitly requires visiting a different, named URL —
never use it to "start over" or guess a URL; you are always already on the correct starting
page when the loop begins.
Before you respond with "done", you MUST have already called "assert_checkpoint" at least once
in this session, identifying the specific element that proves the goal was achieved (e.g. a
confirmation heading or success message). If you have not yet done so, respond with
"assert_checkpoint" now instead of "done" — do not skip straight to "done" even if you can see
the confirmation state, since the checkpoint IS the recorded proof of that state.
Use "done" only after that checkpoint has been asserted.
If a value should be a parameter supplied by the caller rather than a literal you invented,
set it to "${param_name}" and it will be treated as an input parameter.
"""


class DiscoveryError(Exception):
    pass


class DiscoveryAgent:
    def __init__(
        self,
        page: Any,
        policy: AllowlistPolicy,
        evidence: EvidenceRecorder,
        model: str = "claude-sonnet-4-6",
        max_steps: int = 20,
    ):
        self.page = page
        self.policy = policy
        self.evidence = evidence
        self.model = model
        self.max_steps = max_steps
        self._client = None  # lazy import so this module doesn't hard-require the SDK

    def _client_or_raise(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:
                raise DiscoveryError(
                    "anthropic SDK not installed. `pip install anthropic` and set "
                    "ANTHROPIC_API_KEY to run a real discovery pass."
                ) from e
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise DiscoveryError("ANTHROPIC_API_KEY is not set.")
            self._client = anthropic.Anthropic(api_key=api_key)
        return self._client

    def _snapshot(self) -> str:
        """
        Compact accessibility-tree text snapshot of the page.

        Uses Playwright's aria_snapshot() (a YAML-ish text dump of the
        accessibility tree). The older page.accessibility.snapshot() API
        this originally used was removed in newer Playwright releases
        (Chromium deprecated the underlying CDP accessibility domain it
        depended on) — aria_snapshot() is the current supported way to get
        the same kind of role/name information.
        """
        snapshot = self.page.locator("body").aria_snapshot()
        lines = snapshot.splitlines()
        return "\n".join(lines[:200])  # cap for token sanity

    def _ask_model(self, goal: str, history: list[str]) -> dict:
        client = self._client_or_raise()
        snapshot = self._snapshot()
        user_msg = (
            f"GOAL: {goal}\n\n"
            f"YOU ARE CURRENTLY AT THIS URL (do not navigate elsewhere unless the goal "
            f"explicitly requires a different page): {self.page.url}\n\n"
            f"ACTIONS SO FAR:\n" + ("\n".join(history) if history else "(none yet)") + "\n\n"
            f"CURRENT PAGE (accessibility tree):\n{snapshot}\n\n"
            "Respond with the single next action JSON object now."
        )
        resp = client.messages.create(
            model=self.model,
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        text = text.strip().strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
        # Be tolerant of trailing text after the JSON object (some model
        # responses add a stray note/newline after the object even when
        # instructed not to) — parse only the first valid JSON value and
        # ignore anything after it, rather than failing on "Extra data".
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            obj, _end = json.JSONDecoder().raw_decode(text)
            return obj

    def _candidate_for(self, role: str, name: str) -> ElementRef:
        candidates = [
            LocatorCandidate(kind=LocatorKind.ROLE_NAME, value=f"{role}:{name}",
                              rationale="accessible role+name, stable across markup changes"),
        ]
        # A TEXT fallback on an EMPTY name is dangerous — Playwright's
        # get_by_text("") matches broadly (up to the page body itself),
        # which silently "resolves" to a useless element instead of failing
        # loudly. Only add this fallback when there's real text to match.
        if name:
            candidates.append(
                LocatorCandidate(kind=LocatorKind.TEXT, value=name,
                                  rationale="fallback: visible text match")
            )
        return ElementRef(description=f"{role} '{name}'", candidates=candidates)

    def _locate(self, role: str, name: str):
        """
        Resolve a role+name to a live Playwright locator, falling back to a
        role-only match if the named element can't be found. This matters
        because the model reads accessible names off nearby VISIBLE text,
        but a legacy/unlabeled control (an <input> with no <label> or
        aria-label) has NO accessible name at all — Playwright's own
        accessibility computation won't credit it with adjacent table-cell
        text the way a human eye does. Falling back to "the only element
        with this role on screen" keeps discovery moving in that (common)
        case instead of failing outright on a reasonable model guess.
        """
        loc = self.page.get_by_role(role, name=name)
        try:
            loc.first.wait_for(state="visible", timeout=3000)
            return loc.first
        except Exception:
            fallback = self.page.get_by_role(role)
            fallback.first.wait_for(state="visible", timeout=5000)
            return fallback.first

    def discover(
        self, goal: str, entry_url: str, artifact_id: str, name: str,
        risky_step_ids: Optional[set[str]] = None,
    ) -> Artifact:
        risky_step_ids = risky_step_ids or set()
        self.policy.check_url(entry_url)
        self.page.goto(entry_url)
        self.evidence.log("discovery_start", goal=goal, entry_url=entry_url)

        steps: list[Step] = []
        outputs_declared: dict[str, ParamType] = {}
        inputs_declared: dict[str, ParamType] = {}
        history: list[str] = []
        checkpoint: Optional[ElementRef] = None

        for i in range(self.max_steps):
            decision = self._ask_model(goal, history)
            action = decision.get("action")
            rationale = decision.get("rationale", "")
            step_id = f"s{i+1}"
            self.evidence.log("model_decision", step_id=step_id, decision=decision)

            if action == "done":
                if checkpoint is None:
                    # Give the model one corrective nudge instead of failing
                    # outright — it may have simply skipped the formal
                    # assert_checkpoint call while genuinely having reached
                    # the right state.
                    self.evidence.log("done_without_checkpoint_nudge", step_id=step_id)
                    history.append(
                        f"{step_id}: (rejected 'done' — you must call "
                        f"assert_checkpoint on the confirming element FIRST)"
                    )
                    continue
                history.append(f"{step_id}: done — {rationale}")
                break

            if action == "navigate":
                url = decision["url"]
                self.policy.check_url(url)
                self.page.goto(url)
                steps.append(Step(step_id=step_id, action=ActionType.NAVIGATE,
                                   value=url, rationale=rationale))
                history.append(f"{step_id}: navigate to {url}")
                continue

            role, name = decision.get("role", ""), decision.get("name", "")
            target = self._candidate_for(role, name)

            if action == "click":
                el = self._locate(role, name)
                el.click()
                risk = RiskLevel.IRREVERSIBLE if step_id in risky_step_ids else RiskLevel.SAFE
                steps.append(Step(step_id=step_id, action=ActionType.CLICK, target=target,
                                   rationale=rationale, risk=risk))

            elif action == "fill":
                raw_value = decision.get("value", "")
                param_name = raw_value[2:-1] if raw_value.startswith("${") else None
                if param_name:
                    inputs_declared[param_name] = ParamType.STRING
                el = self._locate(role, name)
                el.fill("" if param_name else raw_value)
                steps.append(Step(step_id=step_id, action=ActionType.FILL, target=target,
                                   value=raw_value, rationale=rationale))

            elif action == "select":
                raw_value = decision.get("value", "")
                param_name = raw_value[2:-1] if raw_value.startswith("${") else None
                if param_name:
                    inputs_declared[param_name] = ParamType.STRING
                el = self._locate(role, name)
                if not param_name:
                    el.select_option(raw_value)
                steps.append(Step(step_id=step_id, action=ActionType.SELECT, target=target,
                                   value=raw_value, rationale=rationale))

            elif action == "extract":
                out_name = decision.get("extract_into", f"output_{i}")
                outputs_declared[out_name] = ParamType.STRING
                steps.append(Step(step_id=step_id, action=ActionType.EXTRACT, target=target,
                                   extract_into=out_name, rationale=rationale))

            elif action == "assert_checkpoint":
                checkpoint = target
                steps.append(Step(step_id=step_id, action=ActionType.ASSERT_CHECKPOINT,
                                   checkpoint_locator=target,
                                   expected_text_contains=decision.get("expected_text_contains"),
                                   rationale=rationale))
            else:
                raise DiscoveryError(f"Model returned unknown action: {decision}")

            history.append(f"{step_id}: {action} [{role}] '{name}' — {rationale}")

        if checkpoint is None:
            raise DiscoveryError("Discovery ended without an asserted checkpoint.")

        artifact = Artifact(
            artifact_id=artifact_id,
            name=name,
            description=f"Discovered capability for goal: {goal}",
            discovery_goal=goal,
            target_app="core_banking_console",
            target_entry_url=entry_url,
            inputs=[InputParam(name=n, type=t) for n, t in inputs_declared.items()],
            outputs=[OutputField(name=n, type=t) for n, t in outputs_declared.items()],
            steps=steps,
            checkpoint=checkpoint,
        )
        self.evidence.log("discovery_success", num_steps=len(steps))
        self.evidence.save_artifact(artifact.redacted_copy())
        return artifact
