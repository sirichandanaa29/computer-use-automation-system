# REPORT

## 1. Architecture

Single-process Python system, no queues or services — the brief explicitly
warns against premature scaling infrastructure, and a single tenant/app
pair doesn't need it. Three components share one data contract (the
Artifact schema) and one live Playwright `page` object:

- **DiscoveryAgent** (`agent.py`): observe → decide → act loop. Observes
  via the **accessibility tree**, not screenshots+coordinates. This was
  the key early decision: accessibility role+name is (a) the representation
  that still exists on a desktop app or a screenshot-hostile surface, per
  the brief's "no clean DOM" bias, and (b) exactly what the replay engine's
  preferred locator strategy resolves against — so there's one shared
  vocabulary between "what the model perceived" and "what the artifact
  records," rather than a translation step that could drift. Each model
  turn returns one JSON action; the agent executes it against the real
  page and appends a `Step` with a *ranked* locator candidate list, not a
  raw transcript line.

- **Artifact** (`schema.py`): the typed, versioned, serializable capability
  — the seam between discovery and replay. Discovery writes it; replay
  only ever reads it. Neither component depends on the other's internals.

- **ReplayEngine** (`replay.py`): walks the artifact's steps with no model
  involved, resolving each step's locator candidates in order, checking
  guardrails before every action, detecting declared outcomes, and
  escalating to a human when it can't safely proceed.

Cross-cutting: **guardrails.py** (allowlist + risk classification) and
**escalation.py** (control-transfer model) are used identically by both
discovery and replay — safety isn't a replay-only concern; a discovery run
can also wander somewhere it shouldn't.

Trade-off: putting discovery and replay in one codebase sharing one
`page`-shaped interface (instead of, say, a separate "recorder service"
and "executor service") means less isolation, but it's what keeps the
locator vocabulary honest — I'd rather the two be unable to drift apart
than have a cleaner service boundary.

## 2. Artifact schema

Design goal stated in the brief: it's a **capability contract**, not a step
list. Concretely, the schema is shaped around four things a caller and a
reviewer both need before they'd trust it:

- **Typed `inputs`/`outputs`** — what the agent supplies and gets back,
  independent of how the steps happen to be written. `sensitive: true` on
  a field means the evidence recorder redacts every occurrence of that
  value, at the point of logging (not by trusting call sites).
- **Ranked locator candidates per step** (`LocatorCandidate` list on
  `ElementRef`), not a single selector. Role+name is preferred (stable
  across restyling/markup churn); text and CSS are explicit, labeled
  fallbacks. This is the direct answer to "no test IDs, no clean DOM":
  a step degrades through strategies instead of hard-failing on the first
  one that breaks.
- **`declared_outcomes`** — named, detectable non-success results (member
  not found, validation error, permission denied, session expired) that
  the artifact author anticipated. This turns "is this a business outcome
  or a crash" from a replay-time judgment call into a lookup against
  something the human reviewer already signed off on.
- **`risk` per step** (safe / reversible / irreversible) plus a
  `review_status` (draft/approved) on the artifact — the schema itself
  carries the safety posture, not a side config file someone has to keep
  in sync.

What I left flat rather than modeling more richly: branching flows. Every
artifact here is a linear step list. A real system would eventually need
conditional steps (e.g. "if an interstitial appears, dismiss it, else
continue") — I partially cover this with `DISMISS_IF_PRESENT`, which is
inherently non-fatal-if-absent, but a full branch/goto model was out of
scope for the time box (see Section 7).

## 3. Determinism & error handling

Replay never asks a model to decide anything — every action's target comes
from the artifact's locator candidates, and every value is either a
literal or a `${param}` substitution resolved from the caller's input
dict. The only judgment calls at replay time are: which candidate resolved
first, and which declared outcome (if any) matches the current page.

Result contract has three top-level outcomes, matched to the brief's
explicit taxonomy:

- **`success`** — checkpoint verified, declared outputs extracted.
- **`business_outcome`** — a `DeclaredOutcome` matched (e.g.
  `member_not_found`). Checked *before* each step executes, so a
  not-found page never gets mistaken for a stalled UI.
- **`failure`** — carries `failed_step_id`, `expected`, and `observed` so a
  human debugging it doesn't have to reproduce the run to understand what
  broke.

**Recoverable** conditions (a dismissible interstitial, a bounded wait) are
handled *inside* step execution and never bubble up as a top-level outcome
by themselves — `DISMISS_IF_PRESENT` swallows absence silently by design,
because "the interstitial wasn't there this time" is not noteworthy.

Anything else unexpected — a locator that won't resolve, an unhandled
exception mid-step — routes through one function, `_handle_stuck`, which
snapshots the page HTML as evidence and either escalates to a human (if an
`EscalationManager` is configured) or fails cleanly with that snapshot
attached. There is no silent retry-and-hope path; the only automatic retry
surface is `max_retries`/`timeout_ms` on locator resolution itself, at the
Playwright level (`wait_for(state="visible")`).

Secondary: UI drift specifically is handled the same way as any other
locator miss — by falling through the candidate list, and failing/
escalating with a concrete snapshot if every candidate misses. I did not
build separate "is this drift vs. a runtime error" detection; both surface
identically as "the expected element wasn't found," which is honest given
this project's evidence, and is enough to debug from.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is deliberately at the `ElementRef` /
`LocatorCandidate` boundary, not at the Playwright API boundary. Today
every `LocatorKind` maps to a Playwright call, but nothing in `schema.py`
or the artifact format references Playwright. For a legacy web app (no
clean DOM at all), the same `role_name`/`text` candidates still resolve
via the accessibility tree — no schema change needed, only different
candidate rankings (CSS fallback becomes structural/table-position based,
as in `s8`'s extract step). For a desktop app, `ReplayEngine`'s `_resolve`
would gain a second backend (e.g. an OS accessibility API / UI Automation)
selected by `artifact.target_app`'s declared surface type, but `Step`,
`ElementRef`, and the outcome taxonomy are unchanged — "perceive/act on a
surface" lives entirely behind `_resolve`/`_execute_step`; "the recorded
flow" is the surface-agnostic part.

**Multi-tenant reuse.** `TenantOverlay` (schema.py) is a thin patch
applied at replay time: `locator_overrides` keyed by `step_id`, tried
*before* the base artifact's own candidates, plus an optional entry-URL
override. A base artifact recorded against one tenant's vendor-product
instance stays a shared, versioned asset; a differently-branded/configured
tenant running the same underlying product gets an overlay instead of a
re-recording. This is implemented (`ReplayEngine._candidates_for`) but not
exercised end-to-end in this submission (see Section 7 — the optional
"canonicalization / cross-tenant reuse" stretch goal was cut).

**Drift detection across tenants/versions**, as a design answer rather
than built infrastructure: since every replay already logs which locator
*candidate index* resolved (or that all failed) per step, aggregating that
across replays per `(artifact_id, tenant_id)` would surface "candidate 1
used to resolve, now candidate 2 does" as an early drift signal, and "all
candidates now fail" as a hard drift signal requiring re-review — without
needing to compare screenshots or DOMs directly. I did not build the
aggregation; the log shape it depends on already exists.

## 5. Escalation & handoff

`escalation.py`'s `Session` wraps the single live `page` object and a
`who_controls: "automation" | "human"` flag — the one piece of state that
answers "who's allowed to act right now." `ReplayEngine.session
.require_automation_control()` is checked before every automated action,
so control transfer is enforced, not just documented.

**Detect & route**: any unrecoverable condition during a step (locator
never resolves, an irreversible step lacking pre-approval, an unhandled
exception) goes through `_handle_stuck`, which builds an
`InterventionRequest` carrying the capability name, the exact step ID, a
human-readable reason, and a saved HTML snapshot of the live page at that
moment — enough to orient without re-running anything.

**Take control / hand back**: `EscalationManager.escalate()` flips
`who_controls` to `"human"`, blocks on `operator_fn(request, session)` —
which is handed the *same* `session.page`, not a fresh one — and on return
flips control back to `"automation"` and records a `HumanAction`
(description + whether they actually resolved it). Replay then either
continues at the next step (resolved) or reports a clean `failure` result
(gave up) — never a crash either way.

**What's mocked vs. real, per the brief's scope note**: the operator
*console* is mocked as `cli_operator_fn` — a blocking terminal prompt
rather than a real co-browsing UI. What's real is the mechanism the brief
asked for: pause, cede control of the actual live session, resume, and
record what happened. Swapping `cli_operator_fn` for a real queued
operator UI changes nothing about `Session`, `EscalationManager`, or how
`ReplayEngine` calls it.

## 6. Safety

Two independent, composable mechanisms (`guardrails.py`):

- **`AllowlistPolicy`** — a hard boundary on *where* and *what kind of
  action* is permitted at all: domain allowlist checked before every
  navigation, action-type allowlist checked before every step, plus
  blocked path substrings (e.g. `/admin`) that apply even within an
  allowed domain. Enforced identically during discovery and replay — a
  discovery run can't wander outside the sandbox any more than a replay
  can. A violation is a `PolicyViolation`, always surfaced as a `failure`
  outcome, never silently swallowed.

- **`RiskClassifier`** — orthogonal to the allowlist: classifies *what
  kind* of action a step is (`safe` / `reversible` / `irreversible`), used
  to decide whether it may proceed unattended. Default posture is
  conservative: irreversible steps require `irreversible_preapproved` to
  have been set for the run (a human/reviewer decision made once, at
  artifact-approval time — modeled by `Artifact.review_status`), otherwise
  they escalate rather than either blocking outright (which would make the
  capability useless) or proceeding silently (which would be unsafe). This
  is demonstrated in the reference artifact: step `s7` (submitting the
  sub-account) is marked `irreversible`.

**Data handling**: `EvidenceRecorder` redacts at the point of writing —
every value belonging to an `input`/`output` marked `sensitive: true` is
registered via `mark_sensitive()` before a run starts, and every log line,
failure snapshot, and result file passes through `_redact()` before
touching disk. This means a bug elsewhere that accidentally logs a raw
value still can't leak it, as long as the field was declared sensitive in
the schema — which is enforced at the data-model level, not left to
callers to remember.

**Limits, stated plainly**: the allowlist is domain+path+action-type only
— it doesn't inspect *values* (e.g. it wouldn't block a `fill` step whose
resolved value happens to look like a full account number typed into the
wrong field). Redaction is exact-string-match, not pattern-based, so it
only protects values the schema declared sensitive up front, not
incidentally-sensitive text that shows up elsewhere on a page. Both are
reasonable for a system whose steps are pre-recorded and reviewed rather
than freely improvised at replay time, but neither is a substitute for
review before an artifact reaches `approved`.

## 7. Cuts

What's deliberately thin or stubbed, and why:

- **Operator console** — mocked as a blocking CLI prompt (Section 5). The
  control-transfer model is real; the UI is not, per the brief's explicit
  scope note.
- **Desktop/legacy-web surface implementation** — designed for (Section 4)
  but not built; only the web-app backend is implemented, per the brief's
  "design, not necessarily build" instruction for 3.7.
- **Multi-tenant overlay** — the `TenantOverlay` mechanism is implemented
  in the replay engine but not exercised against a second tenant variant
  in this submission; I judged the artifact schema and replay/escalation
  depth to be the higher-value use of the time box, per "go deep where it
  matters."
- **Branching/conditional flows** beyond `DISMISS_IF_PRESENT` — every
  artifact here is a linear step list. A real system needs conditional
  branches (e.g. two different valid next screens depending on account
  state); the schema doesn't model that yet.
- **Confidence scoring / approval gating on replay** (an optional stretch
  goal) — `review_status: draft|approved` exists on the schema but nothing
  currently reads it to gate unattended replay; only the per-step risk
  check does that today.
- **Multi-run stability signal** (optional stretch goal) — not built.

**What I'd build next**, in priority order: (1) the drift-aggregation
signal described in Section 4, since the logging shape already supports
it cheaply; (2) a second tenant variant exercising `TenantOverlay`
end-to-end, since that's the multi-tenant story's actual proof; (3)
gating unattended replay on `review_status == "approved"`, since it's a
one-line check away from being real; (4) a minimal branching primitive in
the step schema, since linear-only flows are the biggest gap between this
artifact model and a production one.
