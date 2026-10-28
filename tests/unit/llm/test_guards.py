"""Unknown-column guard (Task 6, AC 34) with the val_adj_x demo column."""
from statbrief.analytics.store import ObservationStore
from statbrief.domain.report import Finding, NumericalClaim, ReportDraft, Section
from statbrief.llm.guards import check_unknown_columns
from tests.unit.llm import data

UNDOC = {"val_adj_x"}
STORE = data.store()


def _draft(paragraphs=(), acknowledged=(), claims=(), caveats=(), headline="Unemployment was stable.",
           title="Regional labour market"):
    """A draft with one section; claims are referenced by the section."""
    claims = list(claims)
    return ReportDraft(title=title, executive_summary=[Finding(headline=headline)],
                       sections=[Section(indicator_code="UNEMP_RATE", title="Unemployment", paragraphs=list(paragraphs),
                                         claim_ids=[c.claim_id for c in claims])],
                       claims=claims, citations=[], caveats=list(caveats),
                       unknown_columns_acknowledged=list(acknowledged))


def _claim(cid, text, obs_ids, metric_id=None):
    """A claim citing observations."""
    return NumericalClaim(claim_id=cid, text=text, value=5.3, unit="IDX", metric_id=metric_id,
                          observation_ids=list(obs_ids))


def test_clean_draft_has_no_violations():
    """No mention of the column, nothing acknowledged required beyond listing it."""
    assert check_unknown_columns(_draft(["Unemployment was 5.6 %."], acknowledged=["val_adj_x"]), UNDOC, STORE) == []


def test_unacknowledged_column_is_a_violation():
    """Every undocumented column must be listed in unknown_columns_acknowledged."""
    v = check_unknown_columns(_draft(["Unemployment was 5.6 %."]), UNDOC, STORE)
    assert len(v) == 1 and "val_adj_x" in v[0] and "unknown_columns_acknowledged" in v[0]


def test_mention_without_unknown_wording_is_interpretation():
    """Prose that names the column exactly without saying its meaning is unknown is flagged."""
    v = check_unknown_columns(_draft(["val_adj_x shows seasonally adjusted values."], acknowledged=["val_adj_x"]),
                              UNDOC, STORE)
    assert len(v) == 1 and "interpret" in v[0] and "sections[0].paragraphs[0]" in v[0]


def test_mention_stating_meaning_unknown_is_allowed():
    """'The meaning of val_adj_x is not documented' is the permitted wording (ESMB §4.4)."""
    d = _draft(["The file also has a column val_adj_x; its meaning is not documented."], acknowledged=["val_adj_x"])
    assert check_unknown_columns(d, UNDOC, STORE) == []
    d = _draft(caveats=["The column val_adj_x is undocumented and was not analysed."], acknowledged=["val_adj_x"])
    assert check_unknown_columns(d, UNDOC, STORE) == []


def test_word_boundary_matching():
    """Names inside longer identifiers do not count as mentions."""
    d = _draft(["The series val_adj_xyz and my_val_adj_x are different columns."], acknowledged=["val_adj_x"])
    assert check_unknown_columns(d, UNDOC, STORE) == []


def test_claim_citing_observation_from_column_is_interpretation():
    """A claim built on an observation read from the undocumented column is flagged even if acknowledged."""
    d = _draft(acknowledged=["val_adj_x"], claims=[_claim("c9", "The adjusted index was 5.3.", ["x1"])])
    v = check_unknown_columns(d, UNDOC, STORE)
    assert len(v) == 1 and "claim c9" in v[0] and "val_adj_x" in v[0]


def test_claim_text_mentioning_column_is_interpretation():
    """A claim about the column is interpretation regardless of its wording."""
    d = _draft(acknowledged=["val_adj_x"], claims=[_claim("c2", "val_adj_x (meaning unknown) was 5.3.", ["u01"])])
    v = check_unknown_columns(d, UNDOC, STORE)
    assert any("claim c2" in x for x in v)


def test_mentions_in_title_and_headline_checked():
    """Title and headlines are scanned too."""
    d = _draft(title="What val_adj_x tells us", headline="val_adj_x rose.", acknowledged=["val_adj_x"])
    v = check_unknown_columns(d, UNDOC, STORE)
    assert {x.split(":")[0] for x in v} == {"title", "executive_summary[0].headline"}


def test_no_undocumented_columns_means_no_checks():
    """With nothing undocumented the guard is silent."""
    assert check_unknown_columns(_draft(["val_adj_x is adjusted."]), set(), STORE) == []


# --- review fixes I1 / M2 ---------------------------------------------------------------------------------

COMMON = {"Total", "source", "LAST UPDATE", "unit"}


def test_common_word_headers_do_not_flag_ordinary_prose():
    """I1: bare words that happen to equal a common-word header are not mentions of the column."""
    d = _draft(["Total employment was broadly stable compared with the same month of the previous year.",
                "Source: Eurostat. The unit is percent."],
               caveats=["Figures reflect the last update of the source and may be revised."],
               claims=[_claim("c1", "Total employment rose by 5.3.", ["u01"])], acknowledged=sorted(COMMON))
    assert check_unknown_columns(d, COMMON, STORE) == []


def test_quoted_common_word_header_is_a_mention():
    """I1: a quoted or backticked header names the column unambiguously."""
    for text in ("The column 'Total' shows adjusted totals.", "`LAST UPDATE` marks revisions.",
                 "The “unit” column holds the base year."):
        v = check_unknown_columns(_draft([text], acknowledged=sorted(COMMON)), COMMON, STORE)
        assert len(v) == 1 and "interprets" in v[0], text


def test_identifier_headers_match_case_sensitively():
    """I1: identifier-like headers (underscore, digit, inner capital) match exactly as written."""
    assert check_unknown_columns(_draft(["VAL_ADJ_X shows adjusted values."], acknowledged=["val_adj_x"]),
                                 UNDOC, STORE) == []
    v = check_unknown_columns(_draft(["valAdj2 shows adjusted values."], acknowledged=["valAdj2"]), {"valAdj2"}, STORE)
    assert len(v) == 1


def test_unknown_wording_does_not_excuse_interpretive_verbs():
    """M2: 'whose meaning is not documented, signals ...' still interprets the column."""
    d = _draft(["val_adj_x, whose meaning is not documented, signals strong seasonal demand."],
               acknowledged=["val_adj_x"])
    assert len(check_unknown_columns(d, UNDOC, STORE)) == 1


def test_claim_on_common_word_column_still_caught_by_provenance():
    """I1: for common-word headers the observation rule still catches interpretation."""
    store = ObservationStore([*data.observations(), data._obs("t1", "TOT", "RO", "2024-01", 9.0, column="Total")])
    d = _draft(claims=[_claim("c3", "It was 9.", ["t1"])], acknowledged=sorted(COMMON))
    v = check_unknown_columns(d, COMMON, store)
    assert v == ["claim c3 cites data from undocumented column 'Total': remove it"]
