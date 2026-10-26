"""Curated indicator catalog (D-005, spec AC 8) and uncurated "Advanced" indicator specs.

Every curated dataset and filter code was verified against the live Eurostat / World Bank APIs on
2026-10-26. Deviation from D-005: Eurostat discontinued `prc_hicp_manr` (last period 2025-12, ECOICOP
ver. 2 switch); `HICP_YOY` therefore uses its replacement `prc_hicp_minr` (dimension `coicop18`, total
`TOTAL`). `sts_inpr_m` gained an `indic_bt` dimension, pinned to `PRD` so one series is returned.

Units of curated specs use the canonical codes of `statbrief.ingest.units` where one exists (`%`, `PERS`,
`I21`) so they join uploaded data; units without a canonical equivalent keep an explicit code
(`CLV10_EUR` = chain-linked 2010 volumes in EUR, multiplier 6; `EUR_HAB` = EUR per inhabitant) so they
are never compared with nominal amounts.

Uncurated specs (`advanced_spec`) carry no unit, frequency or rate flag: the unit comes only from the
API response or from the user, and the frequency from the response. Codes are validated because they
become URL path segments and query parameters.
"""
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from statbrief.domain.observation import Frequency, RegionLevel

Source = Literal["eurostat", "worldbank"]
UNCURATED_LABEL = "uncurated"

_EUROSTAT_DATASET = re.compile(r"[A-Za-z0-9_]{1,64}")
_WORLDBANK_INDICATOR = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+){0,9}")
_FILTER_TOKEN = re.compile(r"[A-Za-z0-9_\-]{1,64}")
# Query parameters the client sets itself; a user filter must never override them.
_EUROSTAT_RESERVED = frozenset({"geo", "time", "format", "lang", "sincetimeperiod", "untiltimeperiod",
                                "lasttimeperiod", "firsttimeperiod", "freq"})
_COUNTRY_LEVELS: tuple[RegionLevel, ...] = ("country", "aggregate")


def canonical_filters(filters: Mapping[str, str]) -> str:
    """Key-sorted `k=v` pairs joined by `&` (stable part of series ids, obs ids and cache keys)."""
    return "&".join(f"{k}={filters[k]}" for k in sorted(filters))


@dataclass(frozen=True)
class IndicatorSpec:
    """What to fetch for one indicator and how to label it.

    `dataset` is the Eurostat dataset code or the World Bank indicator id. `unit`, `frequency` and
    `is_rate` are None only for uncurated specs (unknown until the response, never guessed).
    """

    code: str
    label: str
    source: Source
    dataset: str
    filters: Mapping[str, str] = field(default_factory=dict)
    unit: str | None = None
    unit_multiplier: int = 0
    frequency: Frequency | None = None
    is_rate: bool | None = None
    region_levels: tuple[RegionLevel, ...] = _COUNTRY_LEVELS
    curated: bool = True

    def __post_init__(self) -> None:
        """Freeze the filters (read-only mapping) and validate codes."""
        object.__setattr__(self, "filters", MappingProxyType(dict(self.filters)))
        _validate_codes(self.source, self.dataset, self.filters)

    @property
    def canonical_filters(self) -> str:
        """The spec's filters as a canonical `k=v&...` string."""
        return canonical_filters(self.filters)


def _validate_codes(source: str, dataset: str, filters: Mapping[str, str]) -> None:
    """ValueError for an unknown source, unsafe dataset/indicator code, or unsafe/reserved filter."""
    if source == "eurostat":
        if not _EUROSTAT_DATASET.fullmatch(dataset):
            raise ValueError("Eurostat dataset codes contain only letters, digits and '_'")
        for k, v in filters.items():
            if not (_FILTER_TOKEN.fullmatch(k) and _FILTER_TOKEN.fullmatch(v)):
                raise ValueError("filter names and values contain only letters, digits, '_' and '-'")
            if k.lower() in _EUROSTAT_RESERVED:
                raise ValueError(f"filter {k!r} is set by StatBrief itself and cannot be overridden")
    elif source == "worldbank":
        if not _WORLDBANK_INDICATOR.fullmatch(dataset):
            raise ValueError("World Bank indicator ids contain only letters, digits, '_' and '.'")
        if filters:
            raise ValueError("World Bank indicators take no filters")
    else:
        raise ValueError(f"unknown source {source!r}")


CATALOG: dict[str, IndicatorSpec] = {s.code: s for s in (
    IndicatorSpec("HICP_YOY", "HICP inflation, annual rate of change (all items)", "eurostat", "prc_hicp_minr",
                  {"coicop18": "TOTAL", "unit": "RCH_A"}, "%", 0, "M", True),
    IndicatorSpec("UNEMP_RATE", "Unemployment rate, seasonally adjusted (% of labour force)", "eurostat",
                  "une_rt_m", {"s_adj": "SA", "age": "TOTAL", "sex": "T", "unit": "PC_ACT"}, "%", 0, "M", True),
    IndicatorSpec("GDP_VOL", "GDP, chain-linked volumes (2010), seasonally and calendar adjusted", "eurostat",
                  "namq_10_gdp", {"na_item": "B1GQ", "s_adj": "SCA", "unit": "CLV10_MEUR"}, "CLV10_EUR", 6, "Q",
                  False),
    IndicatorSpec("IND_PROD", "Industrial production volume index (B-D, 2021=100)", "eurostat", "sts_inpr_m",
                  {"indic_bt": "PRD", "nace_r2": "B-D", "s_adj": "SCA", "unit": "I21"}, "I21", 0, "M", False),
    IndicatorSpec("GDP_PC_REG", "Regional GDP per inhabitant at current market prices", "eurostat",
                  "nama_10r_2gdp", {"unit": "EUR_HAB"}, "EUR_HAB", 0, "A", False,
                  ("country", "nuts1", "nuts2", "aggregate")),
    IndicatorSpec("WB_GDP_GROWTH", "GDP growth (annual %)", "worldbank", "NY.GDP.MKTP.KD.ZG", {}, "%", 0, "A",
                  True),
    IndicatorSpec("WB_CPI", "Inflation, consumer prices (annual %)", "worldbank", "FP.CPI.TOTL.ZG", {}, "%", 0,
                  "A", True),
    IndicatorSpec("WB_POP", "Population, total", "worldbank", "SP.POP.TOTL", {}, "PERS", 0, "A", False),
)}


def advanced_spec(source: str, code: str, filters: Mapping[str, str], *, unit: str | None = None) -> IndicatorSpec:
    """An uncurated spec for a free-entry dataset/indicator code (AC 8), flagged `curated=False`.

    The unit is taken from the API response, or from `unit` when the user states it; frequency and the
    rate/level flag stay unknown (None). ValueError for unsafe codes or reserved filter names.
    """
    _validate_codes(source, code, filters)
    canon = canonical_filters(filters)
    ind_code = f"{source}:{code}" + (f"?{canon}" if canon else "")
    return IndicatorSpec(ind_code, UNCURATED_LABEL, source, code, filters,  # type: ignore[arg-type]
                         unit=(unit.strip() or None) if unit else None, frequency=None, is_rate=None,
                         region_levels=("country", "nuts1", "nuts2", "nuts3", "aggregate", "other"), curated=False)
