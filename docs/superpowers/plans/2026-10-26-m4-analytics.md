# M4 — Deterministic Analytics and Claim Verification — Plan (lean, fast mode D-026)

> One developer implements all tasks (TDD, commit per task) in worktree `D:/AI_Finance_summary-m4`, branch `m4-analytics` from `main`; one reviewer reviews once.

**Goal:** Compute report metrics with pandas only, never when the data does not support them (recorded skip reasons), flag significant changes, and deterministically verify every numerical claim an LLM makes against the normalized observations (AC 23–27, D-010).

**Constraints:** pandas is the only new dependency (pin exact version; add to requirements.txt AND pyproject.toml — parity test); tunables in Settings (+ .env.example): `significance_rate_pp=1.0`, `significance_level_pct=2.0`, `top_movers=3`, `max_revisions=3`, `max_parse_retries=2`; docstrings everywhere; pure functions, no I/O (input = list[Observation]); no network; commit trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Do not edit docs/PROGRESS.md, CLAUDE.md, .claude/, scripts/resume_check.py, vercel.json.

**Existing:** `statbrief.domain.observation.Observation` (indicator_code, region_code, period "YYYY"/"YYYY-Qn"/"YYYY-MM", frequency A/Q/M, value float|None, unit, unit_multiplier, status_flag, provenance).

## Task 1 — Contracts (`domain/metrics.py`, `domain/report.py`, `domain/review.py`)
- `MetricKind = Literal["latest","yoy","mom","qoq","abs_change","pct_change","pp_change"]`; `metric_id(kind, indicator, region, period[, base_period]) -> str` (`"yoy:HICP_YOY:RO113:2024-03"`) and `parse_metric_id`.
- `MetricResult(metric_id, kind, indicator, region, period, base_period|None, value, unit, observation_ids: list[str], formula: str)` — `formula` human-readable with inputs, e.g. `"(104.2 / 100.9 − 1) × 100 = 3.27"`.
- `MetricSkip(kind, indicator, region, period, reason)`; `SignificantChange(metric_id, magnitude, rule)`.
- `NumericalClaim(claim_id, text, value, unit, metric_id|None, observation_ids: list[str] (min 1))`, `Citation(kind: upload|api|reference, ref_id, label, locator|None)`, `Finding(headline, claim_ids)`, `Section(indicator_code, title, paragraphs, claim_ids, citation_ids)`, `ReportDraft(title, executive_summary (1–5), sections, claims, citations, caveats, unknown_columns_acknowledged)` — exactly as in docs/architecture.md §2.
- `ClaimCheck(claim_id, ok, expected|None, reason|None)`, `VerificationResult(ok, checks, orphan_numbers, feedback)`.

## Task 2 — Metrics (`analytics/metrics.py`)
- `compute_metrics(observations, *, indicator, region) -> (list[MetricResult], list[MetricSkip])` using pandas on one (indicator, region) series sorted by period:
  - `latest` (last non-missing value).
  - `yoy`: needs the same period one year earlier (M: t−12, Q: t−4, A: t−1). **Rates** (unit `%` or `pp`) → percentage-point change `value_t − value_{t−1y}` reported as kind `pp_change` with base; **levels** → `(v_t / v_{t−1y} − 1) × 100` in `%`.
  - `mom` only for monthly; `qoq` only for quarterly (levels: % change; rates: pp).
  - `abs_change` and `pct_change` between first and last period of the requested window (levels only for pct_change).
  - Never compute across different units or unit_multipliers (skip with reason); a gap (None) at either end → skip with reason; zero base → skip (division by zero) with reason.
  - Every result lists its input observation ids and a formula string; values are **not rounded** internally (rounding is a presentation concern).
- `is_rate(unit) -> bool` (`%`, `pp`).
- Tests: monthly rate YoY → pp; level YoY %; MoM not on quarterly (skip reason); missing base → skip; zero base → skip; mixed units → skip; formulas contain inputs; deterministic ordering.

## Task 3 — Significance + lookup (`analytics/significance.py`, `analytics/store.py`)
- `flag_significant(results, settings) -> list[SignificantChange]`: |pp_change| ≥ `significance_rate_pp` for rates; |pct yoy/qoq/mom| ≥ `significance_level_pct` for levels; plus top `top_movers` by |magnitude| across regions for each indicator when ≥ 2 regions.
- `ObservationStore` (in-memory index over a list[Observation]): `get(obs_id)`, `series(indicator, region)`; `compute_metric(metric_id, store) -> MetricResult` (recomputes exactly one metric from its id — used by the verifier and later by LLM tools).
- Tests: thresholds from Settings; top movers; compute_metric equals compute_metrics output for the same id; unknown id → KeyError.

## Task 4 — Claim verifier (`analytics/verify.py`)
- `matches_at_stated_precision(stated_text_number: str, computed: float) -> bool`: precision from the decimals written ("5.3" → ±0.05; "5" → ±0.5; "5,3" with decimal comma accepted); uses half-up rounding of the computed value.
- `extract_numbers(text) -> list[NumberToken(raw, value, start, end)]`: numbers with `.`/`,` decimals, optional sign, optional `%`/`pp`/`p.p.`; excludes citation markers `[1]`, 4-digit years 1900–2100 when not followed by a unit, period tokens (`2024-03`, `Q1`), ordinal/section refs (`§3.2`).
- `verify_draft(draft: ReportDraft, store: ObservationStore) -> VerificationResult`. For each claim:
  1. every `observation_ids` entry exists in the store; 2. if `metric_id` → recompute via `compute_metric` and compare at stated precision; else the claim value must equal the single cited observation's value at stated precision; 3. unit matches (`%` vs `pp` distinguished; `%` claim on a pp metric fails); 4. the claim's `text` contains a number token that equals `claim.value` at its stated precision; 5. region/period mentioned are consistent with the metric id (period string or month name/year present when the claim names a period — best effort, document the rule).
- Orphans: every number token in `executive_summary` headlines and section paragraphs that is not inside a claim text referenced by that finding/section → `orphan_numbers` (with the snippet).
- `feedback`: concise, actionable lines per failure (`"claim c3: stated 5.1 %, recomputed 5.27 % → write 5.3 %"`), used by M7's revise loop.
- Tests: correct claim passes (5.3 vs 5.27); 5.1 vs 5.27 fails with feedback; "5" vs 4.6 passes, "5" vs 4.4 fails; unknown obs id fails; % vs pp mismatch fails; prose number differing from claim.value fails; orphan number detected; years/citation markers/§ refs not orphans; decimal comma claims.

## Done criteria
All tests green; report `D:/AI_Finance_summary-m4/.superpowers/sdd/m4-report.md`.
