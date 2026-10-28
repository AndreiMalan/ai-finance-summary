"""Presentation helpers shared by the PDF and Excel builders: precision, number formatting, provenance text.

Numbers are rounded only here, at presentation, with the precision the claim text states (a claim
"fell to 6.4%" shows one decimal wherever its value appears). A number no claim states uses
`ExportOptions.default_decimals`. Stored and exported raw values (Excel Data / Metrics sheets) stay unrounded.
"""
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from statbrief.analytics.combine import source_label
from statbrief.config import Settings
from statbrief.domain.observation import ApiProvenance, Observation
from statbrief.domain.report import NumericalClaim

# A number written in prose: digits with optional "," / "." groups (protocol, not a tunable).
_NUMBER_TOKEN = re.compile(r"\d+(?:[.,]\d+)*")


@dataclass(frozen=True)
class ExportOptions:
    """Presentation tunables of the builders (from Settings; defaults are the Settings defaults)."""

    default_decimals: int = Settings.model_fields["export_default_decimals"].default
    chart_max_series: int = Settings.model_fields["export_chart_max_series"].default

    @classmethod
    def from_settings(cls, settings: Settings) -> "ExportOptions":
        """Options taken from the app settings."""
        return cls(default_decimals=settings.export_default_decimals,
                   chart_max_series=settings.export_chart_max_series)


def _readings(token: str) -> list[tuple[Decimal, int]]:
    """Possible (value, decimals) readings of a number token ("1,234.5", "6.4", "6,4", "12,345")."""
    seps = [c for c in token if c in ".,"]
    if not seps:
        return [(Decimal(token), 0)]
    out: list[tuple[Decimal, int]] = []
    last = max(token.rfind("."), token.rfind(","))
    decimal_sep = token[last]
    head, tail = token[:last], token[last + 1:]
    if decimal_sep not in head:  # read the last separator as the decimal mark, the other as grouping
        out.append((Decimal(head.replace(".", "").replace(",", "") + "." + tail), len(tail)))
    if len(set(seps)) == 1 and all(len(g) == 3 for g in token.split(seps[0])[1:]):  # all thousands groups
        out.append((Decimal(token.replace(seps[0], "")), 0))
    return out


def claim_decimals(claim: NumericalClaim) -> int | None:
    """Decimals with which the claim text states its value, or None when the text does not state it.

    A written number states the value when the value rounds to it at the written precision (half-even
    or half-up both accepted); the sign is ignored because prose writes "fell by 7.8 points" for -7.8.
    """
    target = Decimal(repr(abs(claim.value)))
    best: int | None = None
    for token in _NUMBER_TOKEN.findall(claim.text):
        for value, decimals in _readings(token):
            half = Decimal(1).scaleb(-decimals) / 2
            if abs(target - value) <= half and (best is None or decimals > best):
                best = decimals
    return best


def format_number(value: float, decimals: int) -> str:
    """Value rounded half-even to `decimals` places with "," thousands grouping ("-1,234.5")."""
    q = Decimal(repr(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_EVEN)
    if q == 0:
        q = abs(q)  # never print "-0.0"
    return f"{q:,.{decimals}f}"


def metric_decimals(metric_id: str, claims: list[NumericalClaim], default: int) -> int:
    """Decimals for a metric: the finest precision of a claim that states it, else `default`."""
    found = [d for c in claims if c.metric_id == metric_id and (d := claim_decimals(c)) is not None]
    return max(found) if found else default


def observation_locator(obs: Observation) -> str:
    """Where an observation lives: "sheet S, row R, column C" for uploads, the query URL for APIs."""
    p = obs.provenance
    if isinstance(p, ApiProvenance):
        return p.query_url
    parts = [f"sheet {p.worksheet}" if p.worksheet else None,
             f"row {p.row_number}" if p.row_number is not None else None, f"column {p.column}"]
    return ", ".join(x for x in parts if x)


def observation_source(obs: Observation) -> str:
    """Human-readable source and locator of an observation ("Upload: f.xlsx (sheet S, row 4, column valoare)")."""
    return f"{source_label(obs)} ({observation_locator(obs)})"


def unit_text(unit: str, multiplier: int) -> str:
    """Unit with its power-of-ten multiplier when non-zero ("RON×10^6")."""
    return f"{unit}×10^{multiplier}" if multiplier else unit
