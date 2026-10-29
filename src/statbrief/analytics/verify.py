"""Deterministic verification of every number an LLM draft states (AC 26, D-010).

Precision rule: a stated number fixes its own precision by the decimals written ("5.3" → 1 decimal, "5" → 0).
The recomputed value is rounded half-up (ties away from zero) at that precision, starting from its shortest
decimal representation (``repr``; metrics are computed in exact decimal arithmetic, so ties such as
5.35 − 5.0 = 0.35 are real ties), and must equal the stated number exactly.

Number tokens (`extract_numbers`) are read the English way (reports are English-only, spec.md:12): optional
sign (+, -, −); ``.`` is the decimal point ("104.235"); ``,`` groups thousands ("12,345", "1,234,567",
"1,234.5"); "1.234,5" and "1.234.567" are unambiguous continental grouping; a comma followed by one or two
digits ("5,3", "5,27") is accepted as a decimal comma. Any other comma use ("1234,567", "5,3456") is
*ambiguous* and can never be verified: the feedback asks for a decimal point and comma grouping.
A token may carry a unit (% / percent / per cent → ``%``; pp / p.p. / percentage point(s) / %-points /
% points → ``pp``; Romanian spellings are accepted best effort) or a glued letter suffix ("1.5x", kept as a
unitless number so it must still be backed by a claim). Not numbers: citation markers ``[1]``, periods
(``2024-03``, ``2024-Q1``, ``Q1``/``T1``/``H1``), dates (``15.03.2024``), section, table and figure references
(``§3.2``, "Section 4"), ordinals ("3rd"), decades ("1990s"), digits glued after letters ("v2", "RO113",
"COVID-19"), and bare years 1900–2100 not followed by a unit, multiplier or currency word — unless the
number is the value the claim itself states ("1950 workers" for a claim of 1950).

Per-claim checks (`verify_draft`):
1. every cited observation id exists;
2. with ``metric_id`` the metric is recomputed (`compute_metric`) and every cited observation must be one of
   its inputs; without it the claim must cite exactly one observation with a value. The stated number (the
   prose token that states ``claim.value``, signed like ``claim.value``; else ``claim.value`` itself) must
   match the recomputed value at its stated precision. A sign-only error gets its own feedback;
3. the claim unit (normalised; Eurostat rate codes such as RCH_A count as ``%``; multiplier included) equals
   the metric/observation unit, and a unit written next to the number in the text must agree (``%`` and
   ``pp`` are distinct);
4. the claim text contains a number token equal to ``claim.value`` at the token's precision. An unsigned token
   matches the magnitude, a signed one must match the sign. Direction words (rose/grew/up/... vs
   fell/eased/down/...; only when the text uses one family): for true changes (``pp_change``,
   ``abs_change``, ``pct_change``) they must agree with the sign of the value. For level growth rates
   (``yoy``/``mom``/``qoq``) "fell to 5.3 %" describes how the rate moved, not its sign, so the check runs
   only when the text states exactly one earlier rate to compare with ("... from 6.1 %"), and then the
   word must agree with the move from that rate; otherwise it is skipped;
5. best-effort period/region consistency: if the text names years, at least one must be the year of the metric
   period or its base; month-year names ("March 2024", "2024-03") are checked for monthly metrics and quarter
   names ("Q1 2024", "2024-Q1") for quarterly ones in the same way; if the text names region codes known to
   the store, one of them must be the claim's region.

Orphans: every number token in the title, finding headlines, section titles/paragraphs and caveats must be
backed by a claim referenced by that same finding (headline) or section (title, paragraphs); the report title
and caveats reference no claims, so numbers there are always orphans. "Backed" means: the token states the
claim's value (sign rule as in check 4), is written with the same number of decimals as the claim text's own
statement of it, has a compatible unit, and each claim backs at most one number per headline/paragraph —
so "5 regions" or a second "5 %" next to a 5.27 % claim are orphans, not coincidental matches.
"""
import re
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import NamedTuple

from statbrief.analytics.metrics import PERIODS_PER_YEAR, rate_unit, shift_period
from statbrief.analytics.store import MetricUnavailable, ObservationStore, compute_metric
from statbrief.domain.metrics import MetricResult, metric_id, parse_metric_id, period_frequency
from statbrief.domain.report import NumericalClaim, ReportDraft
from statbrief.domain.review import ClaimCheck, VerificationResult
from statbrief.ingest.units import normalize_unit

_MINUS = "−"  # U+2212, typographic minus
_SIGN = rf"[+\-{_MINUS}]"
_NUM = (r"\d{1,3}(?:,\d{3})+\.\d+|\d{1,3}(?:,\d{3}){2,}"      # 1,234.5 / 1,234,567
        r"|\d{1,3}(?:\.\d{3})+,\d+|\d{1,3}(?:\.\d{3}){2,}"     # 1.234,5 / 1.234.567
        r"|\d+(?:[.,]\d+)?")                                   # 5 / 5.3 / 12,345 / 5,3 (/ ambiguous 1234,567)
_UNIT_WORDS = {
    "%": "%", "percent": "%", "per cent": "%", "procente": "%", "procent": "%",
    "p.p.": "pp", "pp": "pp", "percentage points": "pp", "percentage point": "pp",
    "%-points": "pp", "%-point": "pp", "% points": "pp", "% point": "pp",
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
    r"(?<!\d)\d+-(?:lea|a)\b",                                    # Romanian ordinals: al 3-lea, a 2-a
    r"§+\s*\d+(?:\.\d+)*",                                        # §3.2
    r"\b(?:sections?|tables?|figures?|fig\.|charts?|annex|appendix|chapter|tabelul|tabel|figura|graficul|"
    r"sec[țt]iunea|anexa|capitolul)\s+\d+(?:\.\d+)*",              # Section 4.1, table 2
)]
_YEAR_MIN, _YEAR_MAX = 1900, 2100  # calendar range treated as years in prose (protocol, not a tunable)
_AFTER_YEAR_VALUE = re.compile(
    r"\s*(?:million|millions|thousand|thousands|billion|billions|mil\.?|mii|mld\.?|lei|ron|eur|euro|usd|"
    r"persons|persoane)\b", re.IGNORECASE)
# English direction words; Romanian forms are best effort only (reports are English, spec.md:12).
_INCREASE = re.compile(r"\b(?:rose|rise|risen|rising|increased?|increasing|grew|grow|growth|up|higher|"
                       r"climbed|accelerated|crescut|cre[șs]tere|majorare|avans)\b", re.IGNORECASE)
_DECREASE = re.compile(r"\b(?:fell|fall|fallen|falling|decreased?|decreasing|declined?|declining|down|lower|"
                       r"dropped|drop|contracted|eased|sc[ăa]zut|sc[ăa]dere|diminuare|redus|reducere)\b",
                       re.IGNORECASE)
_GROWTH_KINDS = frozenset({"yoy", "mom", "qoq"})  # level growth rates: direction words describe the rate's move
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
_AMBIGUOUS_HELP = "write decimals with a decimal point (5.3) and thousands with comma grouping (12,345)"


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
    """Exact decimal value, decimals written and ambiguity of one matched number (English reading)."""
    ambiguous = False
    if "," in num and "." in num:
        dec_sep = "." if num.rfind(".") > num.rfind(",") else ","
    elif num.count(",") > 1 or num.count(".") > 1:
        dec_sep = None  # thousands grouping only
    elif "." in num:
        dec_sep = "."  # English decimal point, any number of decimals ("104.235")
    elif "," in num:
        head, tail = num.split(",")
        if len(tail) == 3 and len(head) <= 3 and not head.startswith("0"):
            dec_sep = None  # English thousands grouping ("12,345")
        else:
            dec_sep = ","  # decimal comma: unambiguous only with 1–2 decimals ("5,3", "5,27")
            ambiguous = len(tail) > 2
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


def extract_numbers(text: str, claimed: Iterable[float] = ()) -> list[NumberToken]:
    """Every number token in `text` that states a quantity (see the module docstring for the exclusions).

    A bare year-like integer is kept when it equals one of `claimed` (the value a claim states).
    """
    claimed_ints = {int(v) for v in claimed if float(v).is_integer()}
    masked = _mask(text)
    tokens = []
    for m in _TOKEN.finditer(masked):
        sign, num, unit_word, suffix = m.group("sign"), m.group("num"), m.group("unit"), m.group("suffix")
        unit = _UNIT_WORDS[unit_word.lower()] if unit_word else None
        is_year = (num.isdigit() and not sign and unit is None and len(num) == 4
                   and _YEAR_MIN <= int(num) <= _YEAR_MAX)
        if suffix and (suffix.lower() in _ORDINAL_SUFFIXES or (is_year and suffix.lower() == "s")):
            continue  # an ordinal ("3rd") or a decade ("1990s")
        if (is_year and not suffix and int(num) not in claimed_ints
                and not _AFTER_YEAR_VALUE.match(masked, m.end())):
            continue  # a bare year
        value, decimals, ambiguous = _parse_number(sign, num)
        tokens.append(NumberToken(raw=text[m.start():m.end()], value=float(value), start=m.start(), end=m.end(),
                                  number=(sign or "") + num, decimals=decimals, unit=unit, signed=bool(sign),
                                  ambiguous=ambiguous))
    return tokens


def _round_half_up(x: float, decimals: int) -> Decimal:
    """`x` rounded half-up (ties away from zero) to `decimals`, from its shortest decimal representation."""
    return Decimal(repr(float(x))).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


def round_half_up(x: float, decimals: int) -> Decimal:
    """Public form of the verifier's rounding rule (half-up, D-010), shared with export presentation."""
    return _round_half_up(x, decimals)


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


def _canonical_unit(unit: str, multiplier: int = 0) -> tuple[str, int]:
    """(code, multiplier) of a unit spelling: prose unit words, rate codes (RCH_A → %), then the ingest normaliser."""
    word = _UNIT_WORDS.get(" ".join(unit.lower().split()))
    if word:
        return word, multiplier
    rate = rate_unit(unit)
    if rate:
        return rate, multiplier
    n = normalize_unit(unit)
    return (n.code, n.multiplier + multiplier) if n else (unit, multiplier)


def _fmt(x: float) -> str:
    """Short display form of an exact value for feedback (presentation only)."""
    return f"{x:.6g}"


def _fmt_at(x: float, decimals: int) -> str:
    """`x` rounded half-up and written with `decimals` decimals."""
    return f"{_round_half_up(x, decimals):f}"


def _decimals_of(x: float) -> int:
    """Decimals in the shortest representation of `x` (0 for integers)."""
    exp = Decimal(repr(float(x))).as_tuple().exponent
    return max(0, -exp) if isinstance(exp, int) else 0


def _unit_label(unit: tuple[str, int]) -> str:
    """Display form of a (code, multiplier) unit."""
    code, mult = unit
    return code if mult == 0 else f"{code} ×10^{mult}"


def _allowed_periods(kind: str | None, period: str, base: str | None) -> set[str]:
    """The period a claim is about plus its comparison base (derived for YoY/MoM/QoQ)."""
    allowed = {period}
    derived = _derived_base(kind, period) if kind else None
    for p in (base, derived):
        if p:
            allowed.add(p)
    return allowed


def _derived_base(kind: str, period: str) -> str | None:
    """The base period a YoY/MoM/QoQ id implies, else None."""
    if kind == "yoy":
        return shift_period(period, -PERIODS_PER_YEAR[period_frequency(period)])
    if kind in ("mom", "qoq"):
        return shift_period(period, -1)
    return None


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
    """What a claim is checked against: value, canonical unit, kind, period, base and region."""

    value: float
    unit: tuple[str, int]
    kind: str | None
    period: str
    base: str | None
    region: str


def _replacement_id(mid: str, store: ObservationStore) -> str | None:
    """For a change id the data refuses on a rate series, the computable pp_change id that states it."""
    try:
        p = parse_metric_id(mid)
    except ValueError:
        return None
    base = p.base_period if p.kind in ("abs_change", "pct_change") else (
        _derived_base(p.kind, p.period) if p.kind in _GROWTH_KINDS else None)
    if base is None:
        return None
    try:
        candidate = metric_id("pp_change", p.indicator, p.region, p.period, base)
        compute_metric(candidate, store)
    except (KeyError, ValueError):
        return None
    return candidate


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
            replacement = _replacement_id(claim.metric_id, store)
            reasons.append(f"metric {claim.metric_id} cannot be computed: {exc.reason}"
                           + (f"; use metric_id {replacement}" if replacement else ""))
            return None
        stray = [i for i in claim.observation_ids if i not in m.observation_ids]
        if stray:
            reasons.append(f"observations {', '.join(stray)} are not used by metric {m.metric_id} "
                           f"(inputs: {', '.join(m.observation_ids)})")
            return None
        return _Target(m.value, _canonical_unit(m.unit, m.unit_multiplier), m.kind, m.period, m.base_period,
                       m.region)
    if len(claim.observation_ids) != 1:
        reasons.append("a claim without metric_id must cite exactly one observation")
        return None
    o = store.get(claim.observation_ids[0])
    if o.value is None:
        reasons.append(f"observation {o.obs_id} has no value")
        return None
    return _Target(o.value, _canonical_unit(o.unit, o.unit_multiplier), None, o.period, None, o.region_code)


def _direction_problem(claim: NumericalClaim, target: _Target, best: NumberToken,
                       tokens: list[NumberToken]) -> str | None:
    """Check 4's direction rule (see the module docstring), or None when consistent or not applicable."""
    if best.signed or target.kind in (None, "latest"):
        return None
    up, down = bool(_INCREASE.search(claim.text)), bool(_DECREASE.search(claim.text))
    if up == down:
        return None  # no direction word, or both families: nothing reliable to check
    if target.kind in _GROWTH_KINDS:
        earlier = [t for t in tokens if t is not best and not t.ambiguous and t.unit == best.unit]
        if len(earlier) != 1:
            return None  # "fell to 5.3 %" without an earlier rate: the word describes an unstated move
        prev = earlier[0].value if earlier[0].signed else abs(earlier[0].value)
        move, what = target.value - prev, f"the rate moved from {_fmt(prev)} to {_fmt(target.value)}"
    else:
        move, what = target.value, f"the change is {_fmt(target.value)}"
    if move != 0 and (up and move < 0 or down and move > 0):
        return f"direction: text says {'an increase' if up else 'a decrease'} but {what}"
    return None


def _check_claim(claim: NumericalClaim, store: ObservationStore) -> tuple[ClaimCheck, str | None, int | None]:
    """Run checks 1–5 on one claim.

    Returns its check, one feedback line (None when it passes) and the decimals of the text's statement of
    the value (None when the text does not state it), which bounds what the claim can back in prose.
    """
    reasons: list[str] = []
    target = _resolve(claim, store, reasons)
    if target is None:
        reason = "; ".join(reasons)
        return (ClaimCheck(claim_id=claim.claim_id, ok=False, reason=reason),
                f"claim {claim.claim_id}: {reason}", None)
    tokens = extract_numbers(claim.text, claimed=(claim.value,))
    stating = [t for t in tokens if _token_states(t, claim.value)]
    if _canonical_unit(claim.unit) != target.unit:
        reasons.append(f"unit {claim.unit} does not match the data unit {_unit_label(target.unit)}")
    unit_ok = [t for t in stating if t.unit is None or t.unit == target.unit[0]]
    best = (unit_ok or stating or [None])[0]
    suggestion = None
    if best is None:
        ambiguous = [t.raw for t in tokens if t.ambiguous]
        found = ", ".join(t.raw for t in tokens) or "no number"
        reasons.append(f"text does not state the claim value {_fmt(claim.value)} (found: {found})"
                       + (f"; {', '.join(ambiguous)} is ambiguous: {_AMBIGUOUS_HELP}" if ambiguous else ""))
        stated_number, stated_value, decimals = repr(claim.value), Decimal(repr(claim.value)), \
            _decimals_of(claim.value)
    else:
        if not unit_ok:
            reasons.append(f"text writes {best.raw} in {best.unit} but the data is in {_unit_label(target.unit)}")
        stated_number, stated_value, decimals = best.number, Decimal(repr(best.value)), best.decimals
        if not best.signed and claim.value < 0:  # "fell 0.3 pp" states -0.3: the sign comes from claim.value
            stated_value = -stated_value
        direction = _direction_problem(claim, target, best, tokens)
        if direction:
            reasons.append(direction)
    unit = _unit_label(target.unit)
    if not _matches(stated_value, decimals, target.value):
        shown = _fmt_at(target.value, decimals)
        if target.value != 0 and _matches(abs(stated_value), decimals, abs(target.value)):
            sign = "negative" if target.value < 0 else "positive"
            text_part = (f"; the text '{best.raw}' can stay" if best is not None and not best.signed
                         else f" and write {shown} in the text")
            reasons.append(f"value sign should be {sign} ({shown} {unit}): set claim.value to "
                           f"{_fmt(target.value)}{text_part}")
        else:
            reasons.append(f"value {stated_number} does not match the recomputed {_fmt(target.value)}")
            suggestion = f"stated {stated_number} {claim.unit}, recomputed {_fmt(target.value)} {unit} → write " \
                         f"{shown} {unit}"
    allowed = _allowed_periods(target.kind, target.period, target.base)
    for problem in (_period_problem(claim.text, allowed, tokens),
                    _region_problem(claim.text, target.region, store.regions)):
        if problem:
            reasons.append(problem)
    stated_decimals = best.decimals if best is not None else None
    if not reasons:
        return ClaimCheck(claim_id=claim.claim_id, ok=True, expected=target.value), None, stated_decimals
    reason = "; ".join(reasons)
    if suggestion and len(reasons) == 1:
        line = f"claim {claim.claim_id}: {suggestion}"
    else:
        line = f"claim {claim.claim_id}: {reason}" + (f" ({suggestion})" if suggestion else "")
    return (ClaimCheck(claim_id=claim.claim_id, ok=False, expected=target.value, reason=reason), line,
            stated_decimals)


def _backing_claim(tok: NumberToken, claims: list[NumericalClaim], decimals: dict[str, int | None],
                   used: set[str]) -> str | None:
    """The id of an unused claim that backs `tok` (see the module docstring), or None."""
    for c in claims:
        if (c.claim_id not in used and decimals.get(c.claim_id) == tok.decimals and _token_states(tok, c.value)
                and (tok.unit is None or tok.unit == _canonical_unit(c.unit)[0])):
            return c.claim_id
    return None


def _prose_units(draft: ReportDraft) -> list[tuple[str, str, list[str]]]:
    """(location, text, ids of the claims that may back its numbers) for every piece of prose in the draft."""
    units: list[tuple[str, str, list[str]]] = [("title", draft.title, [])]
    for i, f in enumerate(draft.executive_summary, 1):
        units.append((f"finding {i} headline", f.headline, f.claim_ids))
    for i, s in enumerate(draft.sections, 1):
        units.append((f"section {i} title", s.title, s.claim_ids))
        units += [(f"section {i} paragraph {j}", p, s.claim_ids) for j, p in enumerate(s.paragraphs, 1)]
    units += [(f"caveat {k}", c, []) for k, c in enumerate(draft.caveats, 1)]
    return units


def _orphan_line(where: str, tok: NumberToken) -> str:
    """Actionable feedback for one unbacked prose number."""
    if tok.ambiguous:
        return f"{where}: '{tok.raw}' is ambiguous: {_AMBIGUOUS_HELP}"
    if where == "title" or where.startswith("caveat"):
        return f"{where}: '{tok.raw}' cannot be backed by a claim here: remove the number or move it to a section"
    owner = "finding" if where.startswith("finding") else "section"
    return (f"{where}: '{tok.raw}' has no supporting claim in this {owner}: add a NumericalClaim stating it with "
            f"the same decimals and list it in this {owner}'s claim_ids, or remove the number")


def verify_draft(draft: ReportDraft, store: ObservationStore) -> VerificationResult:
    """Verify every claim and every prose number of `draft` against `store` (see the module docstring)."""
    checks, lines = [], []
    stated_decimals: dict[str, int | None] = {}
    for claim in draft.claims:
        check, line, decimals = _check_claim(claim, store)
        checks.append(check)
        stated_decimals[claim.claim_id] = decimals
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
        claims = [c for c in draft.claims if c.claim_id in allowed_ids]
        used: set[str] = set()
        for tok in extract_numbers(text):
            backing = _backing_claim(tok, claims, stated_decimals, used)
            if backing is not None:
                used.add(backing)
                continue
            ctx = text[max(0, tok.start - _CONTEXT_CHARS):tok.end + _CONTEXT_CHARS]
            orphans.append(f"{where}: '{tok.raw}' in \"{ctx}\"")
            lines.append(_orphan_line(where, tok))
    ok = all(c.ok for c in checks) and not orphans and not lines
    return VerificationResult(ok=ok, checks=checks, orphan_numbers=orphans, feedback="\n".join(lines))
