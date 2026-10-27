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
    """English reading (I2): ',ddd' groups thousands, '.' is decimal; '5,3' is a decimal comma; others ambiguous."""
    toks = extract_numbers("1,234,567 and 1.234.567,5 and 12,345 and 1.234 and 0.125 and 5,3 and 5,27 and "
                           "1234,567 and 5,3456")
    assert [(t.value, t.ambiguous) for t in toks] == [
        (1234567.0, False), (1234567.5, False), (12345.0, False), (1.234, False), (0.125, False), (5.3, False),
        (5.27, False), (1234.567, True), (5.3456, True)]


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
    """An ambiguous separator ('1234,567') can never be verified; the feedback says how to write it."""
    res = verify_draft(_draft([_claim()], paragraphs=["Inflation reached 5.27 % in March 2024 in 1234,567 homes."]),
                       STORE)
    assert not res.ok and "ambiguous" in res.feedback and "decimal point" in res.feedback


def test_caveat_numbers_checked():
    """Numbers in caveats need a claim too."""
    d = _draft([_claim()])
    res = verify_draft(d.model_copy(update={"caveats": ["Data revised by 0.4 pp."]}), STORE)
    assert not res.ok and any("0.4" in o for o in res.orphan_numbers)


# --- review fixes (regression tests from the review probes) ---------------------------------------------

CPI = [obs("2023-03", 100.0, ind="CPI", unit="I15"), obs("2024-03", 105.27, ind="CPI", unit="I15")]
POP = [obs("2023", 12000.0, ind="POP", unit="PERS"), obs("2024", 12345.0, ind="POP", unit="PERS")]
EMP = [obs("2024", 1950.0, ind="EMP", unit="PERS")]
IDX = [obs("2024", 104.235, ind="IDX", unit="I15")]
REVIEW_STORE = ObservationStore(CPI + POP + EMP + IDX + RATE)
CPI_IDS = ("CPI|RO|2024-03|I15", "CPI|RO|2023-03|I15")
PP_ID = "pp_change:HICP:RO:2024-03:2023-03"


def _cpi(text, value=5.27):
    """A claim on the level YoY yoy:CPI:RO:2024-03 (= 5.27 %)."""
    return _claim(text=text, value=value, metric_id="yoy:CPI:RO:2024-03", ids=CPI_IDS)


@pytest.mark.parametrize("text", ["Annual inflation fell to 5.3 % in March 2024",
                                  "Annual inflation eased to 5.3 % in March 2024",
                                  "Annual inflation fell to 5.3 % in March 2024 from 6.1 %"])
def test_direction_words_on_growth_rates_pass(text):
    """I1: 'fell to 5.3 %' describes how a growth rate moved, not its sign; no false failure."""
    assert _check(verify_draft(_draft([_cpi(text)]), REVIEW_STORE)).ok


def test_direction_on_growth_rate_checked_against_stated_earlier_rate():
    """I1: with an explicit earlier rate the direction word must agree with the move from it."""
    res = verify_draft(_draft([_cpi("Annual inflation rose to 5.3 % in March 2024 from 6.1 %")]), REVIEW_STORE)
    assert not _check(res).ok and "direction" in _check(res).reason


def test_english_thousands_pass():
    """I2: '12,345 persons' is twelve thousand three hundred forty-five."""
    c = _claim(text="The population reached 12,345 persons in 2024", value=12345.0, unit="persons",
               metric_id="latest:POP:RO:2024", ids=("POP|RO|2024|PERS",))
    res = verify_draft(_draft([c]), REVIEW_STORE)
    assert res.ok, res.feedback


def test_three_decimal_value_passes():
    """I2: '104.235' is a decimal number."""
    c = _claim(text="The index stood at 104.235 in 2024", value=104.235, unit="I15", metric_id=None,
               ids=("IDX|RO|2024|I15",))
    res = verify_draft(_draft([c]), REVIEW_STORE)
    assert res.ok, res.feedback


@pytest.mark.parametrize("text,ok", [("rose 0.4 pp in 2024", True), ("rose 0.3 pp in 2024", False)])
def test_rounding_ties_use_half_up_on_exact_value(text, ok):
    """I3: 5.35 - 5.0 = 0.35 exactly, so '0.4' is the half-up rounding."""
    store = ObservationStore([obs("2023", 5.0), obs("2024", 5.35)])
    c = _claim(text=text, value=float(text.split()[1]), unit="pp", metric_id="pp_change:HICP:RO:2024:2023",
               ids=("HICP|RO|2024|%", "HICP|RO|2023|%"))
    assert verify_draft(_draft([c]), store).ok is ok


def test_rounding_tie_percent_change():
    """I3: 100 -> 102.5 is a 2.5 % rise, written '3 %' at zero decimals."""
    store = ObservationStore([obs("2023", 100.0, unit="RON"), obs("2024", 102.5, unit="RON")])
    c = _claim(text="Output grew 3 % in 2024", value=3.0, unit="%", metric_id="yoy:HICP:RO:2024",
               ids=("HICP|RO|2024|RON", "HICP|RO|2023|RON"))
    res = verify_draft(_draft([c]), store)
    assert res.ok, res.feedback


def test_coincidental_prose_numbers_are_orphans():
    """I4: '5 regions' and 'Bucharest 5 %' are not backed by the 5.27 % claim (probe 6)."""
    para = "Annual inflation was 5.3 % in March 2024, and 5 regions exceeded 5.3 % too; Bucharest stood at 5 %."
    res = verify_draft(_draft([_cpi("Annual inflation was 5.3 % in March 2024")], paragraphs=[para]), REVIEW_STORE)
    assert not res.ok
    raws = [o.split("'")[1] for o in res.orphan_numbers]
    assert "5" in raws and "5 %" in raws


def test_title_numbers_have_no_backing_claims():
    """I4: the report title and caveats reference no claims, so a number there is always an orphan."""
    d = _draft([_claim()])
    res = verify_draft(d.model_copy(update={"title": "Inflation at 5.27 %"}), STORE)
    assert not res.ok and any(o.startswith("title") for o in res.orphan_numbers)


def test_sign_only_error_rose():
    """I5: right magnitude, wrong sign of claim.value: the feedback says which sign."""
    c = _claim(text="Inflation rose 0.3 pp year on year", value=-0.27, unit="pp", metric_id=PP_ID,
               ids=(LATEST_ID, BASE_ID))
    res = verify_draft(_draft([c]), STORE)
    assert not res.ok and "claim c1: value sign should be positive" in res.feedback


def test_sign_only_error_fell():
    """I5: mirror case, 'fell 0.7 pp' with claim.value +0.73 on a -0.73 change."""
    c = _claim(text="Inflation fell 0.7 pp from February", value=0.73, unit="pp",
               metric_id="pp_change:HICP:RO:2024-03:2024-02", ids=(LATEST_ID, "HICP|RO|2024-02|%"))
    res = verify_draft(_draft([c]), STORE)
    assert not res.ok and "claim c1: value sign should be negative (-0.7 pp)" in res.feedback
    assert "write -0.7" not in res.feedback


def test_year_like_value_is_a_number_when_claimed():
    """Minor M2: '1950 workers' states the claimed 1950, not a year."""
    c = _claim(text="Employment was 1950 workers in 2024", value=1950.0, unit="persons",
               metric_id="latest:EMP:RO:2024", ids=("EMP|RO|2024|PERS",))
    assert _check(verify_draft(_draft([c]), REVIEW_STORE)).ok


def test_singular_percentage_point_unit():
    """Minor M1: 'percentage point' (singular) is pp, as a claim unit and in prose."""
    c = _claim(text="Inflation rose 0.3 percentage point year on year", value=0.27, unit="percentage point",
               metric_id=PP_ID, ids=(LATEST_ID, BASE_ID))
    res = verify_draft(_draft([c]), STORE)
    assert res.ok, res.feedback


def test_percent_points_spelling_is_pp():
    """Minor M3: '%-points' and '% points' are percentage points."""
    assert [t.unit for t in extract_numbers("up 12.5%-points and 3 % points")] == ["pp", "pp"]


def test_unavailable_metric_feedback_names_replacement_id():
    """Minor M4: a YoY id on a rate tells the LLM the exact pp_change id to use."""
    res = verify_draft(_draft([_claim(metric_id="yoy:HICP:RO:2024-03")]), STORE)
    assert PP_ID in res.feedback


def test_eurostat_rate_unit_claim_in_percent():
    """I6: a latest value of an RCH_A series may be claimed in %."""
    store = ObservationStore([obs("2024", 5.27, unit="RCH_A")])
    c = _claim(text="Inflation was 5.3 % in 2024", value=5.27, unit="%", metric_id="latest:HICP:RO:2024",
               ids=("HICP|RO|2024|RCH_A",))
    res = verify_draft(_draft([c]), store)
    assert res.ok, res.feedback
