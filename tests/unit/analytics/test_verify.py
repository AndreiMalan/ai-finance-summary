"""Tests for the deterministic claim verifier (AC 26, D-010)."""
import pytest

from statbrief.analytics.store import ObservationStore
from statbrief.analytics.verify import extract_numbers, matches_at_stated_precision, verify_draft
from statbrief.domain.report import Citation, Finding, NumericalClaim, ReportDraft, Section
from tests.unit.analytics.helpers import monthly, obs

# Monthly HICP rate for RO: 2023-03 = 5.0 %, 2024-03 = 5.27 % -> YoY = 0.27 pp; levels for GDP (annual).
RATE = monthly([5.0] * 12 + [6.6, 6.0, 5.27], start_year=2023)
GDP = [obs("2023", 100.9, ind="GDP", unit="I15"), obs("2024", 104.2, ind="GDP", unit="I15")]  # YoY 3.2705 %
BG = [obs("2024-03", 4.0, reg="BG")]
STORE = ObservationStore(RATE + GDP + BG)
LATEST_ID = "HICP|RO|2024-03|%"
BASE_ID = "HICP|RO|2023-03|%"
GDP_IDS = ["GDP|RO|2024|I15", "GDP|RO|2023|I15"]


def _claim(cid="c1", text="Inflation reached 5.27 % in March 2024", value=5.27, unit="%",
           metric_id="latest:HICP:RO:2024-03", ids=(LATEST_ID,)):
    """A claim (defaults: a correct latest-value claim)."""
    return NumericalClaim(claim_id=cid, text=text, value=value, unit=unit, metric_id=metric_id,
                          observation_ids=list(ids))


def _draft(claims, paragraphs=None, headline=None):
    """A one-finding, one-section draft whose prose repeats the claim texts unless given."""
    claims = list(claims)
    ids = [c.claim_id for c in claims]
    paragraphs = paragraphs if paragraphs is not None else [" ".join(c.text for c in claims)]
    return ReportDraft(
        title="Inflation report 2024", executive_summary=[Finding(headline=headline or claims[0].text,
                                                                  claim_ids=ids)],
        sections=[Section(indicator_code="HICP", title="Inflation", paragraphs=paragraphs, claim_ids=ids,
                          citation_ids=["f1"])],
        claims=claims, citations=[Citation(kind="upload", ref_id="f1", label="irs.xlsx", locator=None)],
        caveats=[])


def _check(result, cid="c1"):
    """The check for one claim."""
    return next(c for c in result.checks if c.claim_id == cid)


# --- precision matching ---------------------------------------------------------------------------------

@pytest.mark.parametrize("stated,computed,ok", [
    ("5.3", 5.27, True), ("5.1", 5.27, False), ("5", 4.6, True), ("5", 4.4, False), ("5,3", 5.27, True),
    ("5.27", 5.2705, True), ("5.28", 5.2705, False), ("2.68", 2.675, True),   # half-up on the decimal repr
    ("-0.3", -0.27, True), ("0.3", -0.27, False), ("−0.3", -0.27, True), ("5", 5.5, False), ("6", 5.5, True),
    ("1,234.5", 1234.46, True), ("1.234,5", 1234.46, True), ("0", 0.4, True), ("0", 0.5, False),
])
def test_matches_at_stated_precision(stated, computed, ok):
    """The computed value, rounded half-up to the stated decimals, must equal the stated number."""
    assert matches_at_stated_precision(stated, computed) is ok


def test_matches_rejects_non_numbers():
    """A string that is not one number is an error, not a silent mismatch."""
    with pytest.raises(ValueError):
        matches_at_stated_precision("abc", 1.0)


# --- number extraction ----------------------------------------------------------------------------------

def _values(text):
    """(value, unit) of every extracted token."""
    return [(t.value, t.unit) for t in extract_numbers(text)]


def test_extract_numbers_basic_units_and_signs():
    """Numbers with decimals, signs and %/pp units are found with their positions."""
    toks = extract_numbers("Inflation rose 0.4 pp to 5,3% (−1.2 p.p. in BG; +2 percentage points in X).")
    assert [(t.raw, t.value, t.unit) for t in toks] == [
        ("0.4 pp", 0.4, "pp"), ("5,3%", 5.3, "%"), ("−1.2 p.p.", -1.2, "pp"), ("+2 percentage points", 2.0, "pp")]
    t = toks[0]
    assert "Inflation rose 0.4 pp to 5,3% (−1.2 p.p. in BG; +2 percentage points in X)."[t.start:t.end] == t.raw


@pytest.mark.parametrize("text", [
    "see [1] and [2, 3]", "in 2024", "between 2023-2024", "in 2024-03", "in 2024-Q1", "in Q1 2024", "§3.2",
    "Section 4.1 and table 2", "the 3rd largest", "on 15.03.2024", "in T1 2024", "from 1999 to 2100",
])
def test_extract_numbers_exclusions(text):
    """Citation markers, bare years, periods, dates, ordinals and section references are not numbers."""
    assert extract_numbers(text) == []


def test_extract_numbers_year_with_unit_kept_and_no_partial_matches():
    """A year-like number with a unit is a value; '5.3x' is the number 5.3 (never '5'); codes are not numbers."""
    assert _values("2024 % and 2050 million RON") == [(2024.0, "%"), (2050.0, None)]
    assert _values("growth 5.3x, v2, RO113, COVID-19, the 1990s, al 3-lea") == [(5.3, None)]
    assert _values("between 5-6 % and 5.3.1") == [(5.0, None), (6.0, "%")]


def test_extract_numbers_grouping_and_ambiguity():
    """Unambiguous thousands grouping is parsed; a lone ',ddd' or '.ddd' grouping is flagged ambiguous."""
    toks = extract_numbers("1,234,567 and 1.234.567,5 and 12,345 and 1.234 and 0.125")
    assert [(t.value, t.ambiguous) for t in toks] == [
        (1234567.0, False), (1234567.5, False), (12.345, True), (1.234, True), (0.125, False)]


# --- claim checks ---------------------------------------------------------------------------------------

def test_correct_claims_pass():
    """Correct latest, pp and level claims pass; the stated rounding 5.3 vs 5.27 is fine."""
    claims = [
        _claim(),
        _claim("c2", text="up 0.3 pp year on year in March 2024", value=0.27, unit="pp",
               metric_id="pp_change:HICP:RO:2024-03:2023-03", ids=(LATEST_ID, BASE_ID)),
        _claim("c3", text="GDP volume grew by 3.3 % in 2024", value=3.3, unit="%", metric_id="yoy:GDP:RO:2024",
               ids=GDP_IDS),
        _claim("c4", text="Inflation stood at 5.3 %", value=5.3, unit="%", metric_id=None, ids=(LATEST_ID,)),
    ]
    res = verify_draft(_draft(claims), STORE)
    assert res.ok, res.feedback
    assert _check(res, "c3").expected == pytest.approx(3.2705, abs=1e-4)
    assert res.feedback == "" and res.orphan_numbers == []


def test_wrong_value_fails_with_actionable_feedback():
    """5.1 vs a recomputed 5.27 fails and the feedback says what to write."""
    res = verify_draft(_draft([_claim(text="Inflation reached 5.1 % in March 2024", value=5.1)]), STORE)
    assert not res.ok and not _check(res).ok and _check(res).expected == 5.27
    assert "claim c1: stated 5.1 %, recomputed 5.27 % → write 5.3 %" in res.feedback


@pytest.mark.parametrize("stated,value,ok", [("5", 4.6, True), ("5", 4.4, False)])
def test_integer_precision(stated, value, ok):
    """'5' accepts values that round to 5 and rejects others."""
    store = ObservationStore([obs("2024", value, unit="RON")])
    c = _claim(text=f"Value was {stated} RON in 2024", value=float(stated), unit="RON", metric_id=None,
               ids=("HICP|RO|2024|RON",))
    assert verify_draft(_draft([c]), store).ok is ok


def test_decimal_comma_claim():
    """Decimal commas in prose are accepted."""
    c = _claim(text="Inflația a ajuns la 5,3 % în martie 2024", value=5.3)
    assert verify_draft(_draft([c]), STORE).ok


def test_unknown_observation_fails():
    """Every cited observation must exist."""
    res = verify_draft(_draft([_claim(ids=(LATEST_ID, "obs_nope"))]), STORE)
    assert not res.ok and "obs_nope" in _check(res).reason


def test_observation_not_used_by_metric_fails():
    """A claim may only cite observations the metric was computed from."""
    res = verify_draft(_draft([_claim(ids=("BG|RO|x",))]), ObservationStore(RATE + [obs("2024", 1.0, oid="BG|RO|x")]))
    assert not res.ok and "not used by" in _check(res).reason


def test_unknown_metric_fails():
    """A metric the data cannot support fails with the skip reason."""
    res = verify_draft(_draft([_claim(metric_id="yoy:HICP:RO:2024-03")]), STORE)
    assert not res.ok and "pp_change" in _check(res).reason


def test_percent_vs_pp_mismatch_fails():
    """A '%' claim on a pp metric fails, both via the claim unit and via the unit written in the text."""
    c = _claim(text="up 0.27 % year on year in March 2024", value=0.27, unit="%",
               metric_id="pp_change:HICP:RO:2024-03:2023-03", ids=(LATEST_ID, BASE_ID))
    res = verify_draft(_draft([c]), STORE)
    assert not res.ok and "unit" in _check(res).reason
    c2 = c.model_copy(update={"unit": "pp"})
    res2 = verify_draft(_draft([c2]), STORE)
    assert not res2.ok and "%" in _check(res2).reason


def test_prose_number_differing_from_claim_value_fails():
    """The claim text must state the claim's value."""
    res = verify_draft(_draft([_claim(text="Inflation reached 6.1 % in March 2024")]), STORE)
    assert not res.ok and "text" in _check(res).reason


def test_direction_word_contradicting_sign_fails():
    """'fell' with a positive change is a wrong statement even when the magnitude is right."""
    c = _claim(text="fell 0.3 pp year on year in March 2024", value=0.27, unit="pp",
               metric_id="pp_change:HICP:RO:2024-03:2023-03", ids=(LATEST_ID, BASE_ID))
    res = verify_draft(_draft([c]), STORE)
    assert not res.ok and "direction" in _check(res).reason


def test_unsigned_number_for_negative_change():
    """'fell 0.7 pp' states a negative change; the same magnitude with the wrong claim sign fails."""
    feb = "HICP|RO|2024-02|%"
    c = _claim(text="fell 0.7 pp in March 2024 from February 2024", value=-0.73, unit="pp",
               metric_id="pp_change:HICP:RO:2024-03:2024-02", ids=(LATEST_ID, feb))
    assert verify_draft(_draft([c]), STORE).ok
    wrong = c.model_copy(update={"text": "changed by 0.7 pp in March 2024", "value": 0.73})
    res = verify_draft(_draft([wrong]), STORE)
    assert not res.ok and _check(res).expected == pytest.approx(-0.73)


def test_signed_token_must_match_sign():
    """An explicit minus on a positive change fails."""
    c = _claim(text="changed by -0.3 pp in March 2024", value=-0.27, unit="pp",
               metric_id="pp_change:HICP:RO:2024-03:2023-03", ids=(LATEST_ID, BASE_ID))
    assert not verify_draft(_draft([c]), STORE).ok


def test_period_mismatch_fails():
    """A claim naming a different period than its metric fails; naming none is fine."""
    res = verify_draft(_draft([_claim(text="Inflation reached 5.27 % in March 2023")]), STORE)
    assert not res.ok and "period" in _check(res).reason
    res = verify_draft(_draft([_claim(text="Inflation reached 5.27 % in 2022")]), STORE)
    assert not res.ok
    assert verify_draft(_draft([_claim(text="Inflation reached 5.27 %")]), STORE).ok
    assert verify_draft(_draft([_claim(text="Inflation reached 5.27 % in 2024-03")]), STORE).ok


def test_region_mismatch_fails():
    """A claim naming another known region code fails."""
    res = verify_draft(_draft([_claim(text="In BG inflation reached 5.27 % in March 2024")]), STORE)
    assert not res.ok and "region" in _check(res).reason


def test_claim_without_metric_needs_one_observation():
    """Without a metric id a claim restates exactly one observation."""
    c = _claim(text="Inflation stood at 5.27 %", metric_id=None, ids=(LATEST_ID, BASE_ID))
    assert not verify_draft(_draft([c]), STORE).ok


# --- orphans and references ----------------------------------------------------------------------------

def test_orphan_number_detected():
    """A prose number no referenced claim backs is an orphan."""
    res = verify_draft(_draft([_claim()], paragraphs=["Inflation reached 5.27 % in March 2024, up from 7.2 %."]),
                       STORE)
    assert not res.ok and len(res.orphan_numbers) == 1 and "7.2" in res.orphan_numbers[0]
    assert "7.2" in res.feedback


def test_orphan_number_in_headline_detected():
    """Headline numbers are checked against the finding's own claims."""
    res = verify_draft(_draft([_claim()], headline="Inflation at 5.27 %, 3 regions above 6 %"), STORE)
    assert not res.ok and len(res.orphan_numbers) == 2


def test_years_citations_and_section_refs_are_not_orphans():
    """Years, citation markers, periods and section references never count as orphans."""
    para = "In March 2024 (2024-03) inflation reached 5.27 % [1], see §3.2 and Section 4 for 2023-2024."
    assert verify_draft(_draft([_claim()], paragraphs=[para]), STORE).ok


def test_number_backed_by_claim_of_other_section_is_orphan():
    """Coverage is per finding/section: a claim must be referenced where its number appears."""
    c1, c2 = _claim(), _claim("c2", text="Inflation stood at 5.0 % in March 2023", value=5.0,
                              metric_id=None, ids=(BASE_ID,))
    d = _draft([c1, c2])
    sec = d.sections[0].model_copy(update={"claim_ids": ["c1"],
                                           "paragraphs": ["5.27 % in March 2024 after 5.0 % a year earlier"]})
    res = verify_draft(d.model_copy(update={"sections": [sec]}), STORE)
    assert not res.ok and any("5.0" in o for o in res.orphan_numbers)


def test_dangling_claim_reference_fails():
    """A finding or section referring to an unknown claim id fails."""
    d = _draft([_claim()])
    bad = d.model_copy(update={"executive_summary": [Finding(headline="h", claim_ids=["c9"])]})
    res = verify_draft(bad, STORE)
    assert not res.ok and "c9" in res.feedback


def test_ambiguous_number_in_prose_fails():
    """A number whose grouping is ambiguous ('12,345') can never be verified and must be rewritten."""
    res = verify_draft(_draft([_claim()], paragraphs=["Inflation reached 5.27 % in March 2024 in 12,345 homes."]),
                       STORE)
    assert not res.ok and "ambiguous" in res.feedback


def test_caveat_numbers_checked():
    """Numbers in caveats need a claim too."""
    d = _draft([_claim()])
    res = verify_draft(d.model_copy(update={"caveats": ["Data revised by 0.4 pp."]}), STORE)
    assert not res.ok and any("0.4" in o for o in res.orphan_numbers)
