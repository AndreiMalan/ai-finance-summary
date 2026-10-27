"""Deterministic verification of every number an LLM draft states (AC 26, D-010).

Precision rule: a stated number fixes its own precision by the decimals written ("5.3" → 1 decimal, "5" → 0).
The recomputed value is rounded half-up (ties away from zero) at that precision, starting from its shortest
decimal representation (``repr``), and must equal the stated number exactly. "5,3" (decimal comma) is accepted.

Number tokens (`extract_numbers`): optional sign (+, -, −), digits with an optional ``.``/``,`` decimal part or
unambiguous thousands grouping ("1,234,567", "1,234.5", "1.234,5"), an optional unit (% / percent /
procent(e) → ``%``; pp / p.p. / percentage point(s) / puncte procentuale → ``pp``) or a glued letter suffix
("1.5x", kept as a unitless number so it must still be backed by a claim). Not numbers: citation
markers ``[1]``, periods (``2024-03``, ``2024-Q1``, ``Q1``/``T1``/``H1``), dates (``15.03.2024``), section, table
and figure references (``§3.2``, "Section 4"), ordinals ("3rd", "al 3-lea"), decades ("1990s"), digits glued
after letters ("v2", "RO113", "COVID-19"), and bare years 1900–2100 not followed by a unit, multiplier or
currency word. A single grouping-like separator followed by exactly three digits ("12,345", "1.234") is
*ambiguous* (thousands or decimals?) and can never be verified: it must be rewritten.

Per-claim checks (`verify_draft`):
1. every cited observation id exists;
2. with ``metric_id`` the metric is recomputed (`compute_metric`) and every cited observation must be one of
   its inputs; without it the claim must cite exactly one observation with a value. The stated number (the
   prose token that states ``claim.value``, else ``claim.value`` itself) must match the recomputed value at
   its stated precision;
3. the claim unit (normalised, multiplier included) equals the metric/observation unit, and a unit written
   next to the number in the text must agree (``%`` and ``pp`` are distinct);
4. the claim text contains a number token equal to ``claim.value`` at the token's precision. An unsigned token
   matches the magnitude, a signed one must match the sign. For change metrics, if the text uses only
   increase words (rose, grew, up, crescut, ...) the value must be positive; only decrease words (fell,
   declined, down, scăzut, ...) → negative;
5. best-effort period/region consistency: if the text names years, at least one must be the year of the metric
   period or its base; month-year names ("March 2024", "martie 2024", "2024-03") are checked for monthly
   metrics and quarter names ("Q1 2024", "2024-Q1") for quarterly ones in the same way; if the text names
   region codes known to the store, one of them must be the claim's region.

Orphans: every number token in the title, finding headlines, section titles/paragraphs and caveats must be
backed by a claim — for a headline, one of that finding's claims; for a section paragraph, one of that
section's claims; for the titles and caveats, any claim of the draft. "Backed" means the token equals the
claim's value at the token's precision (sign rule as in check 4) with a compatible unit.
"""
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import NamedTuple

from statbrief.analytics.metrics import PERIODS_PER_YEAR, shift_period
from statbrief.analytics.store import MetricUnavailable, ObservationStore, compute_metric
from statbrief.domain.metrics import MetricResult, period_frequency
from statbrief.domain.report import NumericalClaim, ReportDraft
from statbrief.domain.review import ClaimCheck, VerificationResult
from statbrief.ingest.units import normalize_unit

_MINUS = "−"  # U+2212, typographic minus
_SIGN = rf"[+\-{_MINUS}]"
_NUM = (r"\d{1,3}(?:,\d{3})+\.\d+|\d{1,3}(?:,\d{3}){2,}"      # 1,234.5 / 1,234,567
        r"|\d{1,3}(?:\.\d{3})+,\d+|\d{1,3}(?:\.\d{3}){2,}"     # 1.234,5 / 1.234.567
        r"|\d+(?:[.,]\d+)?")                                   # 5 / 5.3 / 5,3 (and the ambiguous 12,345)
_UNIT_WORDS = {
    "%": "%", "percent": "%", "per cent": "%", "procente": "%", "procent": "%",
    "p.p.": "pp", "pp": "pp", "percentage points": "pp", "percentage point": "pp",
    "puncte procentuale": "pp", "punct procentual": "pp",
}
_UNIT_RE = "|".join(re.escape(u) for u in sorted(_UNIT_WORDS, key=len, reverse=True))
_TOKEN = re.compile(
    rf"(?:(?<![\w.,])(?P<sign>{_SIGN}))?(?<![\w.,])(?<![^\W\d_]-)(?P<num>{_NUM})"
    rf"(?:\s?(?P<unit>{_UNIT_RE})(?=\W|$)|(?P<suffix>[^\W\d_]+))?(?![.,]?\d)", re.IGNORECASE)
_ORDINAL_SUFFIXES = frozenset({"st", "nd", "rd", "th"})  # 1st, 2nd, 3rd, 4th are not quantities
_STATED = re.compile(rf"\s*(?P<sign>{_SIGN})?(?P<num>{_NUM})\s*")
_MASKS = [re.compile(p, re.IGNORECASE) for p in (
    r"\[\s*\d+(?:\s*[,;–-]\s*\d+)*\s*\]",                         # citation markers [1], [2, 3]
    r"(?<!\d)\d{4}-(?:Q[1-4]|0[1-9]|1[0-2])(?:-\d{2})?(?!\d)",    # 2024-03, 2024-Q1, 2024-03-15
    r"(?<!\d)\d{1,2}[./]\d{1,2}[./]\d{2,4}(?!\d)",                # 15.03.2024, 15/03/24
    r"(?<!\d)\d{1,2}/\d{4}(?!\d)",                                # 03/2024
    r"(?<!\d)\d+-(?:lea|a)\b",                                      # Romanian ordinals: al 3-lea, a 2-a
    r"§+\s*\d+(?:\.\d+)*",                                        # §3.2
    r"\b(?:sections?|tables?|figures?|fig\.|charts?|annex|appendix|chapter|tabelul|tabel|figura|graficul|"
    r"sec[țt]iunea|anexa|capitolul)\s+\d+(?:\.\d+)*",              # Section 4.1, table 2
)]
_YEAR_MIN, _YEAR_MAX = 1900, 2100  # calendar range treated as years in prose (protocol, not a tunable)
_AFTER_YEAR_VALUE = re.compile(
    r"\s*(?:million|millions|thousand|thousands|billion|billions|mil\.?|mii|mld\.?|lei|ron|eur|euro|usd|"
    r"persons|persoane)\b", re.IGNORECASE)
_INCREASE = re.compile(r"\b(?:rose|rise|risen|rising|increased?|increasing|grew|grow|growth|up|higher|"
                       r"climbed|accelerated|crescut|cre[șs]tere|majorare|avans)\b", re.IGNORECASE)
_DECREASE = re.compile(r"\b(?:fell|fall|fallen|falling|decreased?|decreasing|declined?|declining|down|lower|"
                       r"dropped|drop|contracted|eased|sc[ăa]zut|sc[ăa]dere|diminuare|redus|reducere)\b",
                       re.IGNORECASE)
_MONTHS = {
    m: i + 1 for i, names in enumerate([
        "january jan ianuarie ian", "february feb februarie", "march mar martie", "april apr aprilie",
        "may mai", "june jun iunie", "july jul iulie", "august aug", "september sep sept septembrie",
        "october oct octombrie", "november nov noiembrie", "december dec decembrie"])
    for m in names.split()}
_MONTH_YEAR = re.compile(rf"\b({'|'.join(sorted(_MONTHS, key=len, reverse=True))})\.?\s+(\d{{4}})\b",
                         re.IGNORECASE)
_QUARTER = re.compile(r"\b[QT]([1-4])\s+(\d{4})\b|\b(\d{4})\s*-?\s*Q([1-4])\b")
_ISO_MONTH = re.compile(r"(?<!\d)(\d{4})-(0[1-9]|1[0-2])(?![\d-])")
_YEAR = re.compile(r"(?<![\d.,])(\d{4})(?!\d|[.,]\d)")
_CONTEXT_CHARS = 30  # characters of context around an orphan number in reports (presentation only)


class NumberToken(NamedTuple):
    """One number found in prose: raw text, value, span, decimals written, unit, sign and ambiguity flags."""

    raw: str
    value: float
    start: int
    end: int
    number: str  # the numeric part as written, sign included (no unit)
    decimals: int
    unit: str | None  # "%" or "pp" when written next to the number
    signed: bool
    ambiguous: bool


def _parse_number(sign: str | None, num: str) -> tuple[Decimal, int, bool]:
    """Exact decimal value, decimals written and ambiguity of one matched number."""
    ambiguous = False
    if "," in num and "." in num:
        dec_sep = "." if num.rfind(".") > num.rfind(",") else ","
    elif num.count(",") > 1 or num.count(".") > 1:
        dec_sep = None  # thousands grouping only
    elif "," in num or "." in num:
        dec_sep = "," if "," in num else "."
        head, tail = num.split(dec_sep)
        ambiguous = len(tail) == 3 and not head.startswith("0")
    else:
        dec_sep = None
    if dec_sep is None:
        intpart, frac = num.replace(",", "").replace(".", ""), ""
    else:
        head, frac = num.rsplit(dec_sep, 1)
        intpart = head.replace(",", "").replace(".", "")
    value = Decimal(f"{intpart}.{frac}" if frac else intpart)
    if sign in ("-", _MINUS):
        value = -value
    return value, len(frac), ambiguous


def _mask(text: str) -> str:
    """The text with citation markers, periods, dates and section references blanked (offsets preserved)."""
    for pattern in _MASKS:
        text = pattern.sub(lambda m: " " * len(m.group(0)), text)
    return text


def extract_numbers(text: str) -> list[NumberToken]:
    """Every number token in `text` that states a quantity (see the module docstring for the exclusions)."""
    masked = _mask(text)
    tokens = []
    for m in _TOKEN.finditer(masked):
        sign, num, unit_word, suffix = m.group("sign"), m.group("num"), m.group("unit"), m.group("suffix")
        unit = _UNIT_WORDS[unit_word.lower()] if unit_word else None
        is_year = (num.isdigit() and not sign and unit is None and len(num) == 4
                   and _YEAR_MIN <= int(num) <= _YEAR_MAX)
        if suffix and (suffix.lower() in _ORDINAL_SUFFIXES or (is_year and suffix.lower() == "s")):
            continue  # an ordinal ("3rd") or a decade ("1990s")
        if is_year and not suffix and not _AFTER_YEAR_VALUE.match(masked, m.end()):
            continue  # a bare year
        value, decimals, ambiguous = _parse_number(sign, num)
        tokens.append(NumberToken(raw=text[m.start():m.end()], value=float(value), start=m.start(), end=m.end(),
                                  number=(sign or "") + num, decimals=decimals, unit=unit, signed=bool(sign),
                                  ambiguous=ambiguous))
    return tokens


def _round_half_up(x: float, decimals: int) -> Decimal:
    """`x` rounded half-up (ties away from zero) to `decimals`, from its shortest decimal representation."""
    return Decimal(repr(float(x))).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


def _matches(stated: Decimal, decimals: int, computed: float) -> bool:
    """True when `computed` rounded half-up at `decimals` equals `stated`."""
    try:
        return _round_half_up(computed, decimals) == stated
    except InvalidOperation:
        return False


def matches_at_stated_precision(stated_text_number: str, computed: float) -> bool:
    """Whether `computed` rounds (half-up) to the number written in `stated_text_number` at its precision.

    Raises ValueError when the string is not exactly one number.
    """
    m = _STATED.fullmatch(stated_text_number or "")
    if not m:
        raise ValueError(f"not a number: {stated_text_number!r}")
    value, decimals, _ = _parse_number(m.group("sign"), m.group("num"))
    return _matches(value, decimals, computed)


def _token_states(tok: NumberToken, value: float) -> bool:
    """Whether a prose token states `value`: unsigned tokens state the magnitude, signed ones the signed value."""
    target = value if tok.signed else abs(value)
    return not tok.ambiguous and _matches(Decimal(repr(abs(tok.value) if not tok.signed else tok.value)),
                                          tok.decimals, target)


def _canonical_unit(unit: str) -> tuple[str, int]:
    """(code, multiplier) of a unit spelling, using the ingestion normaliser."""
    n = normalize_unit(unit)
    return (n.code, n.multiplier) if n else (unit, 0)


def _fmt(x: float) -> str:
    """Short display form of an exact value for feedback (presentation only)."""
    return f"{x:.6g}"


def _fmt_at(x: float, decimals: int) -> str:
    """`x` rounded half-up and written with `decimals` decimals."""
    return f"{_round_half_up(x, decimals):f}"


def _allowed_periods(kind: str | None, period: str, base: str | None) -> set[str]:
    """The period a claim is about plus its comparison base (derived for YoY/MoM/QoQ)."""
    allowed = {period}
    if base:
        allowed.add(base)
    elif kind == "yoy":
        allowed.add(shift_period(period, -PERIODS_PER_YEAR[period_frequency(period)]))
    elif kind in ("mom", "qoq"):
        allowed.add(shift_period(period, -1))
    return allowed


def _period_problem(text: str, allowed: set[str], tokens: list[NumberToken]) -> str | None:
    """Why the periods named in `text` contradict `allowed`, or None (best effort, see module docstring)."""
    value_spans = [(t.start, t.end) for t in tokens]
    years = {m.group(1) for m in _YEAR.finditer(text)
             if _YEAR_MIN <= int(m.group(1)) <= _YEAR_MAX
             and not any(s <= m.start() < e for s, e in value_spans)}
    allowed_years = {p[:4] for p in allowed}
    if years and not years & allowed_years:
        return f"period: text names {', '.join(sorted(years))} but the data is for {', '.join(sorted(allowed))}"
    freq = period_frequency(next(iter(allowed)))
    if freq == "M":
        named = {f"{y}-{_MONTHS[mo.lower()]:02d}" for mo, y in _MONTH_YEAR.findall(text)}
        named |= {f"{y}-{mo}" for y, mo in _ISO_MONTH.findall(text)}
    elif freq == "Q":
        named = {f"{a or c}-Q{b or d}" for b, a, c, d in _QUARTER.findall(text)}
    else:
        named = set()
    if named and not named & allowed:
        return f"period: text names {', '.join(sorted(named))} but the data is for {', '.join(sorted(allowed))}"
    return None


def _region_problem(text: str, region: str, known: frozenset[str]) -> str | None:
    """Why the region codes named in `text` contradict `region`, or None."""
    named = {r for r in known if re.search(rf"(?<!\w){re.escape(r)}(?!\w)", text)}
    if named and region not in named:
        return f"region: text names {', '.join(sorted(named))} but the data is for {region}"
    return None


class _Target(NamedTuple):
    """What a claim is checked against: value, unit, kind, period, base and region."""

    value: float
    unit: tuple[str, int]
    kind: str | None
    period: str
    base: str | None
    region: str


def _resolve(claim: NumericalClaim, store: ObservationStore, reasons: list[str]) -> _Target | None:
    """Checks 1–2 (sources): recompute the claim's value from its metric or single observation."""
    missing = [i for i in claim.observation_ids if i not in store]
    if missing:
        reasons.append(f"unknown observation ids: {', '.join(missing)}")
        return None
    if claim.metric_id:
        try:
            m: MetricResult = compute_metric(claim.metric_id, store)
        except MetricUnavailable as exc:
            reasons.append(f"metric {claim.metric_id} cannot be computed: {exc.reason}")
            return None
        stray = [i for i in claim.observation_ids if i not in m.observation_ids]
        if stray:
            reasons.append(f"observations {', '.join(stray)} are not used by metric {m.metric_id} "
                           f"(inputs: {', '.join(m.observation_ids)})")
            return None
        return _Target(m.value, (m.unit, m.unit_multiplier), m.kind, m.period, m.base_period, m.region)
    if len(claim.observation_ids) != 1:
        reasons.append("a claim without metric_id must cite exactly one observation")
        return None
    o = store.get(claim.observation_ids[0])
    if o.value is None:
        reasons.append(f"observation {o.obs_id} has no value")
        return None
    return _Target(o.value, (o.unit, o.unit_multiplier), None, o.period, None, o.region_code)


def _check_claim(claim: NumericalClaim, store: ObservationStore) -> tuple[ClaimCheck, str | None]:
    """Run checks 1–5 on one claim; returns its check and one feedback line (None when it passes)."""
    reasons: list[str] = []
    target = _resolve(claim, store, reasons)
    if target is None:
        reason = "; ".join(reasons)
        return ClaimCheck(claim_id=claim.claim_id, ok=False, reason=reason), f"claim {claim.claim_id}: {reason}"
    tokens = extract_numbers(claim.text)
    stating = [t for t in tokens if _token_states(t, claim.value)]
    claim_unit = _canonical_unit(claim.unit)
    if claim_unit != target.unit:
        reasons.append(f"unit {claim.unit} does not match the data unit {_unit_label(target.unit)}")
    unit_ok = [t for t in stating if t.unit is None or t.unit == target.unit[0]]
    # The stated number is the prose token that states claim.value; without one, claim.value at its own precision.
    stated_number, stated_value, stated_decimals = repr(claim.value), Decimal(repr(claim.value)), \
        _decimals_of(claim.value)
    value_line = None
    if not stating:
        reasons.append(f"text does not state the claim value {_fmt(claim.value)} (found: "
                       f"{', '.join(t.raw for t in tokens) or 'no number'})")
    elif not unit_ok:
        reasons.append(f"text writes {stating[0].raw} in {stating[0].unit} but the data is in "
                       f"{_unit_label(target.unit)}")
    if stating:
        best = (unit_ok or stating)[0]
        stated_number, stated_value, stated_decimals = best.number, Decimal(repr(best.value)), best.decimals
        if not best.signed and claim.value < 0:  # "fell 0.3 pp" states -0.3: the sign comes from claim.value
            stated_value = -stated_value
        if not best.signed and target.kind not in (None, "latest") and target.value != 0:
            up, down = bool(_INCREASE.search(claim.text)), bool(_DECREASE.search(claim.text))
            if up != down and (up and target.value < 0 or down and target.value > 0):
                reasons.append(f"direction: text says {'an increase' if up else 'a decrease'} but the change "
                               f"is {_fmt(target.value)}")
    if not _matches(stated_value, stated_decimals, target.value):
        value_line = (f"claim {claim.claim_id}: stated {stated_number} {claim.unit}, recomputed "
                      f"{_fmt(target.value)} {_unit_label(target.unit)} → write "
                      f"{_fmt_at(target.value, stated_decimals)} {_unit_label(target.unit)}")
        reasons.append(f"value {stated_number} does not match the recomputed {_fmt(target.value)}")
    allowed = _allowed_periods(target.kind, target.period, target.base)
    for problem in (_period_problem(claim.text, allowed, tokens),
                    _region_problem(claim.text, target.region, store.regions)):
        if problem:
            reasons.append(problem)
    if not reasons:
        return ClaimCheck(claim_id=claim.claim_id, ok=True, expected=target.value), None
    reason = "; ".join(reasons)
    others = [r for r in reasons if not r.startswith("value ")]
    line = value_line if value_line and not others else (
        f"claim {claim.claim_id}: {reason}" + (f" ({value_line.split(': ', 1)[1]})" if value_line else ""))
    return ClaimCheck(claim_id=claim.claim_id, ok=False, expected=target.value, reason=reason), line


def _decimals_of(x: float) -> int:
    """Decimals in the shortest representation of `x` (0 for integers)."""
    exp = Decimal(repr(float(x))).as_tuple().exponent
    return max(0, -exp) if isinstance(exp, int) else 0


def _unit_label(unit: tuple[str, int]) -> str:
    """Display form of a (code, multiplier) unit."""
    code, mult = unit
    return code if mult == 0 else f"{code} ×10^{mult}"


def _covered(tok: NumberToken, claims: list[NumericalClaim]) -> bool:
    """Whether a prose token is backed by one of `claims` (value at the token's precision, compatible unit)."""
    return any(_token_states(tok, c.value) and (tok.unit is None or tok.unit == _canonical_unit(c.unit)[0])
               for c in claims)


def _prose_units(draft: ReportDraft) -> list[tuple[str, str, list[str] | None]]:
    """(location, text, allowed claim ids or None for any claim) for every piece of prose in the draft."""
    units: list[tuple[str, str, list[str] | None]] = [("title", draft.title, None)]
    for i, f in enumerate(draft.executive_summary, 1):
        units.append((f"finding {i} headline", f.headline, f.claim_ids))
    for i, s in enumerate(draft.sections, 1):
        units.append((f"section {i} title", s.title, None))
        units += [(f"section {i} paragraph {j}", p, s.claim_ids) for j, p in enumerate(s.paragraphs, 1)]
    units += [(f"caveat {k}", c, None) for k, c in enumerate(draft.caveats, 1)]
    return units


def verify_draft(draft: ReportDraft, store: ObservationStore) -> VerificationResult:
    """Verify every claim and every prose number of `draft` against `store` (see the module docstring)."""
    checks, lines = [], []
    for claim in draft.claims:
        check, line = _check_claim(claim, store)
        checks.append(check)
        if line:
            lines.append(line)
    known = {c.claim_id for c in draft.claims}
    for where, ids in ([(f"finding {i}", f.claim_ids) for i, f in enumerate(draft.executive_summary, 1)]
                       + [(f"section {i}", s.claim_ids) for i, s in enumerate(draft.sections, 1)]):
        for cid in ids:
            if cid not in known:
                lines.append(f"{where} references unknown claim {cid}: add the claim or remove the reference")
    orphans = []
    for where, text, allowed_ids in _prose_units(draft):
        claims = draft.claims if allowed_ids is None else [c for c in draft.claims if c.claim_id in allowed_ids]
        for tok in extract_numbers(text):
            if _covered(tok, claims):
                continue
            ctx = text[max(0, tok.start - _CONTEXT_CHARS):tok.end + _CONTEXT_CHARS]
            orphans.append(f"{where}: '{tok.raw}' in \"{ctx}\"")
            if tok.ambiguous:
                lines.append(f"{where}: '{tok.raw}' is ambiguous (thousands separator or decimals?): write it "
                             f"without grouping or with a different number of decimals")
            else:
                lines.append(f"{where}: '{tok.raw}' has no supporting claim: add a NumericalClaim and list it "
                             f"in this {'finding' if 'finding' in where else 'section'}'s claim_ids, or remove "
                             f"the number")
    ok = all(c.ok for c in checks) and not orphans and not lines
    return VerificationResult(ok=ok, checks=checks, orphan_numbers=orphans, feedback="\n".join(lines))
