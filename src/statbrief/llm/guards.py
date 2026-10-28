"""Unknown-column guard (AC 34, ESMB Guidelines §4.4): undocumented columns are never interpreted.

`check_unknown_columns` returns human-readable violations that the M7 revise loop feeds back to the model
next to the verifier feedback. Rules, for every undocumented column:
1. it must be listed in ``unknown_columns_acknowledged`` (case-insensitive);
2. a sentence of the title, a headline, a section title/paragraph or a caveat that *names* the column must
   say that its meaning is unknown ("its meaning is not documented") and must not use interpretive verbs
   ("signals", "indicates", "reflects", ...) — otherwise it interprets the column;
3. no claim may name the column, and no claim may cite an observation read from it (upload provenance
   column, looked up in the report's ObservationStore — the store is required so this rule is never off).

When does text *name* a column (decision D-033)? Plain prose words must not trip the guard: a header such
as "Total", "source", "unit" or "LAST UPDATE" is also an ordinary word, and the model cannot avoid it.
- Identifier-like headers (containing ``_`` or a digit, or a capital after a lower-case letter, e.g.
  ``val_adj_x``, ``Q17``, ``valAdj``) are named by an exact, case-sensitive, whole-identifier match.
- Any header is named when it is quoted or back-ticked exactly ('Total', "Total", `Total`, “Total”).
- Bare occurrences of a common-word header never count; for those columns rules 1 and 3 (provenance) apply.
The wording check in rule 2 is a heuristic: it can reject an innocent sentence (costing a revision), but a
sentence that names the column and assigns it a meaning is caught.
"""
import re
from collections.abc import Iterable

from statbrief.analytics.store import ObservationStore
from statbrief.domain.observation import UploadProvenance
from statbrief.domain.report import ReportDraft

# Phrases that state a column's meaning is unknown (protocol wording, mirrors the system prompt rule 8).
_UNKNOWN_WORDING = re.compile(
    r"\b(?:not\s+documented|undocumented|meaning\s+(?:is\s+)?unknown|unknown\s+meaning|meaning\s+is\s+not\s+"
    r"(?:known|documented|described)|no\s+(?:documentation|definition|metadata)|not\s+(?:analy[sz]ed|interpreted))\b",
    re.IGNORECASE)
# Verbs that assign a meaning to a column even next to "not documented" wording.
_INTERPRETIVE = re.compile(
    r"\b(?:signal|signals|signalled|signaled|indicate|indicates|indicated|reflect|reflects|reflected|measure|"
    r"measures|measured|show|shows|showed|suggest|suggests|suggested|represent|represents|represented|"
    r"capture|captures|track|tracks|mean|means|denote|denotes|correspond|corresponds|proxy|proxies)\b",
    re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_IDENTIFIER_LIKE = re.compile(r"[_0-9]|[a-z][A-Z]")
_QUOTES = (("'", "'"), ('"', '"'), ("`", "`"), ("‘", "’"), ("“", "”"))


def is_identifier_like(column: str) -> bool:
    """True for headers that cannot be ordinary words (underscore, digit or an inner capital)."""
    return bool(_IDENTIFIER_LIKE.search(column))


def _mention_pattern(column: str) -> re.Pattern[str]:
    """Pattern that matches text *naming* the column (see module docstring)."""
    name = re.escape(column)
    quoted = "|".join(re.escape(o) + name + re.escape(c) for o, c in _QUOTES)
    if is_identifier_like(column):
        return re.compile(rf"(?<![\w]){name}(?![\w])|{quoted}")
    return re.compile(quoted)


def _prose(draft: ReportDraft) -> Iterable[tuple[str, str]]:
    """(location, text) of every prose field."""
    yield "title", draft.title
    for i, f in enumerate(draft.executive_summary):
        yield f"executive_summary[{i}].headline", f.headline
    for i, s in enumerate(draft.sections):
        yield f"sections[{i}].title", s.title
        for j, p in enumerate(s.paragraphs):
            yield f"sections[{i}].paragraphs[{j}]", p
    for i, c in enumerate(draft.caveats):
        yield f"caveats[{i}]", c


def _interprets(sentence: str) -> bool:
    """A sentence naming a column interprets it unless it only states that the meaning is unknown."""
    return not _UNKNOWN_WORDING.search(sentence) or bool(_INTERPRETIVE.search(sentence))


def _column_of(obs_id: str, store: ObservationStore) -> str | None:
    """The uploaded column an observation was read from (None for API data or unknown ids)."""
    if obs_id not in store:
        return None
    prov = store.get(obs_id).provenance
    return prov.column if isinstance(prov, UploadProvenance) else None


def check_unknown_columns(draft: ReportDraft, undocumented: set[str], store: ObservationStore) -> list[str]:
    """Violations of the unknown-column rules (empty list = the draft respects them).

    `store` is the report's ObservationStore; it is required so the provenance rule (3) always runs.
    """
    violations: list[str] = []
    acknowledged = {a.strip().lower() for a in draft.unknown_columns_acknowledged}
    for column in sorted(undocumented):
        pat = _mention_pattern(column)
        if column.lower() not in acknowledged:
            violations.append(f"column '{column}' has no documented meaning: list it in "
                              "unknown_columns_acknowledged")
        for where, text in _prose(draft):
            if any(pat.search(s) and _interprets(s) for s in _SENTENCE_END.split(text)):
                violations.append(f"{where}: interprets undocumented column '{column}'; only state that its "
                                  "meaning is not documented")
        for claim in draft.claims:
            if pat.search(claim.text):
                violations.append(f"claim {claim.claim_id} interprets undocumented column '{column}': remove it")
    for claim in draft.claims:
        cols = sorted({c for oid in claim.observation_ids
                       if (c := _column_of(oid, store)) is not None and c in undocumented})
        for column in cols:
            violations.append(f"claim {claim.claim_id} cites data from undocumented column '{column}': remove it")
    return violations
