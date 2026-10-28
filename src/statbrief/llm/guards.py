"""Unknown-column guard (AC 34, ESMB Guidelines §4.4): undocumented columns are never interpreted.

`check_unknown_columns` returns human-readable violations that the M7 revise loop feeds back to the model
next to the verifier feedback. Rules, for every undocumented column name (matched case-insensitively and
as a whole identifier, so ``val_adj_x`` does not match ``val_adj_xyz``):
1. it must be listed in ``unknown_columns_acknowledged``;
2. a sentence of the title, a headline, a section title/paragraph or a caveat that names it must say that
   its meaning is unknown (e.g. "its meaning is not documented"); any other mention is an interpretation;
3. no claim may name it or cite an observation read from it (provenance column, when a store is given).
The wording check in rule 2 is a heuristic: it can reject an innocent sentence (costing a revision), but a
sentence that assigns a meaning without any "unknown" wording is always caught.
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
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _pattern(column: str) -> re.Pattern[str]:
    """Whole-identifier, case-insensitive match of a column name."""
    return re.compile(rf"(?<![\w]){re.escape(column)}(?![\w])", re.IGNORECASE)


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


def _column_of(obs_id: str, store: ObservationStore | None) -> str | None:
    """The uploaded column an observation was read from (None for API data or unknown ids)."""
    if store is None or obs_id not in store:
        return None
    prov = store.get(obs_id).provenance
    return prov.column if isinstance(prov, UploadProvenance) else None


def check_unknown_columns(draft: ReportDraft, undocumented: set[str],
                          store: ObservationStore | None = None) -> list[str]:
    """Violations of the unknown-column rules (empty list = the draft respects them)."""
    violations: list[str] = []
    acknowledged = {a.strip().lower() for a in draft.unknown_columns_acknowledged}
    lowered = {c.lower(): c for c in undocumented}
    for column in sorted(undocumented):
        pat = _pattern(column)
        if column.lower() not in acknowledged:
            violations.append(f"column '{column}' has no documented meaning: list it in "
                              "unknown_columns_acknowledged")
        for where, text in _prose(draft):
            if any(pat.search(s) and not _UNKNOWN_WORDING.search(s) for s in _SENTENCE_END.split(text)):
                violations.append(f"{where}: interprets undocumented column '{column}'; only state that its "
                                  "meaning is not documented")
        for claim in draft.claims:
            if pat.search(claim.text):
                violations.append(f"claim {claim.claim_id} interprets undocumented column '{column}': remove it")
    for claim in draft.claims:
        cols = sorted({lowered[c.lower()] for oid in claim.observation_ids
                       if (c := _column_of(oid, store)) is not None and c.lower() in lowered})
        for column in cols:
            violations.append(f"claim {claim.claim_id} cites data from undocumented column '{column}': remove it")
    return violations
