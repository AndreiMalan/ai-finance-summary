"""Observation builders shared by the analytics tests."""
from statbrief.domain.observation import Observation, UploadProvenance

PROV = UploadProvenance(file_id="f1", filename="irs.xlsx", worksheet="S", row_number=2, column="valoare")


def obs(period, value, *, ind="HICP", reg="RO", unit="%", mult=0, oid=None):
    """One observation; the frequency is inferred from the period format and the id from its parts."""
    freq = "Q" if "-Q" in period else "M" if "-" in period else "A"
    return Observation(obs_id=oid or f"{ind}|{reg}|{period}|{unit}", indicator_code=ind, region_code=reg,
                       region_level="country", period=period, frequency=freq, value=value, unit=unit,
                       unit_multiplier=mult, provenance=PROV)


def monthly(values, *, start_year=2023, start_month=1, **kw):
    """Consecutive monthly observations starting at start_year-start_month."""
    out = []
    for i, v in enumerate(values):
        y, m = divmod(start_month - 1 + i, 12)
        out.append(obs(f"{start_year + y}-{m + 1:02d}", v, **kw))
    return out
