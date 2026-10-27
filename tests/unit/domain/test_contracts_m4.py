"""Tests for the M4 contracts: metric ids and results, report drafts, verification results."""
import math

import pytest
from pydantic import ValidationError

from statbrief.domain.metrics import (
    MetricResult, MetricSkip, ParsedMetricId, SignificantChange, metric_id, parse_metric_id,
)
from statbrief.domain.report import Citation, Finding, NumericalClaim, ReportDraft, Section
from statbrief.domain.review import ClaimCheck, ReviewDecision, VerificationResult


def test_metric_id_format_and_roundtrip():
    """Ids follow kind:indicator:region:period[:base] and parse back to their parts."""
    mid = metric_id("yoy", "HICP_YOY", "RO113", "2024-03")
    assert mid == "yoy:HICP_YOY:RO113:2024-03"
    assert parse_metric_id(mid) == ParsedMetricId("yoy", "HICP_YOY", "RO113", "2024-03", None)
    pp = metric_id("pp_change", "HICP_YOY", "RO113", "2024-03", "2023-03")
    assert pp == "pp_change:HICP_YOY:RO113:2024-03:2023-03"
    assert parse_metric_id(pp).base_period == "2023-03"


@pytest.mark.parametrize("args", [
    ("bogus", "I", "R", "2024"),                 # unknown kind
    ("yoy", "I:X", "R", "2024"),                 # separator inside a part
    ("yoy", "I", "", "2024"),                    # empty part
    ("yoy", "I", "R", "2024-13"),                # malformed period
    ("pct_change", "I", "R", "2024"),            # window kinds need a base period
    ("yoy", "I", "R", "2024", "2023"),           # derived kinds must not carry one
    ("abs_change", "I", "R", "2024-03", "2023"),  # base must have the same frequency
])
def test_metric_id_rejects_invalid(args):
    """Malformed ids are rejected when built."""
    with pytest.raises(ValueError):
        metric_id(*args)


@pytest.mark.parametrize("bad", ["", "yoy:I:R", "yoy:I:R:2024:2023", "nope:I:R:2024", "pp_change:I:R:2024"])
def test_parse_metric_id_rejects_invalid(bad):
    """Malformed id strings are rejected when parsed."""
    with pytest.raises(ValueError):
        parse_metric_id(bad)


def _result(**kw):
    """A valid metric result, overriding fields."""
    base = dict(metric_id="yoy:I:R:2024", kind="yoy", indicator="I", region="R", period="2024", base_period=None,
                value=3.27, unit="%", observation_ids=["a", "b"], formula="(104.2 / 100.9 − 1) × 100 = 3.27")
    base.update(kw)
    return MetricResult(**base)


def test_metric_result_frozen_and_consistent():
    """Results are immutable and their id must match their own fields."""
    r = _result()
    with pytest.raises(ValidationError):
        r.value = 1.0
    with pytest.raises(ValidationError):
        _result(metric_id="yoy:I:R:2023")
    with pytest.raises(ValidationError):
        _result(value=math.nan)
    with pytest.raises(ValidationError):
        _result(observation_ids=[])


def test_skip_and_significant_change():
    """Skips carry a reason; significant changes carry a rule."""
    s = MetricSkip(kind="mom", indicator="I", region="R", period="2024-Q1", reason="MoM needs monthly data")
    assert s.reason
    with pytest.raises(ValidationError):
        MetricSkip(kind="mom", indicator="I", region="R", period="2024-Q1", reason="")
    c = SignificantChange(metric_id="yoy:I:R:2024", magnitude=3.2, rule="level_threshold")
    assert c.magnitude == 3.2


def _claim(cid="c1", **kw):
    """A valid claim, overriding fields."""
    base = dict(claim_id=cid, text="Inflation reached 5.3 %", value=5.3, unit="%", metric_id=None,
                observation_ids=["obs_1"])
    base.update(kw)
    return NumericalClaim(**base)


def test_claim_requires_observation_and_finite_value():
    """A claim cites at least one observation and has a finite value."""
    with pytest.raises(ValidationError):
        _claim(observation_ids=[])
    with pytest.raises(ValidationError):
        _claim(value=math.inf)


def test_report_draft_shape_and_unique_claim_ids():
    """The executive summary has 1–5 findings and claim ids are unique."""
    sec = Section(indicator_code="HICP", title="Inflation", paragraphs=["p"], claim_ids=["c1"], citation_ids=["f1"])
    cit = Citation(kind="upload", ref_id="f1", label="irs.xlsx", locator="Inflation!B14")
    ok = ReportDraft(title="T", executive_summary=[Finding(headline="h", claim_ids=["c1"])], sections=[sec],
                     claims=[_claim()], citations=[cit], caveats=[])
    assert ok.unknown_columns_acknowledged == []
    with pytest.raises(ValidationError):
        ok.model_validate({**ok.model_dump(), "executive_summary": []})
    with pytest.raises(ValidationError):
        ok.model_validate({**ok.model_dump(), "executive_summary": [{"headline": "h", "claim_ids": []}] * 6})
    with pytest.raises(ValidationError):
        ok.model_validate({**ok.model_dump(), "claims": [_claim().model_dump()] * 2})
    with pytest.raises(ValidationError):
        Citation(kind="web", ref_id="x", label="y", locator=None)


def test_verification_contracts():
    """Checks and results hold what the revise loop needs."""
    chk = ClaimCheck(claim_id="c1", ok=False, expected=5.27, reason="stated 5.1")
    res = VerificationResult(ok=False, checks=[chk], orphan_numbers=["7.2 in section 1"], feedback="fix c1")
    assert not res.ok and res.checks[0].expected == 5.27
    assert ReviewDecision(action="approve").comment is None
