"""Presentation precision and provenance text."""
import pytest

from statbrief.config import Settings
from statbrief.domain.report import NumericalClaim
from statbrief.export.bundle import ReportBundle
from statbrief.export.format import (ExportOptions, claim_decimals, format_number, metric_decimals,
                                     observation_locator)


def _claim(text: str, value: float, metric_id: str | None = None) -> NumericalClaim:
    """A claim with one dummy observation."""
    return NumericalClaim(claim_id="c", text=text, value=value, unit="%", metric_id=metric_id,
                          observation_ids=["o1"])


@pytest.mark.parametrize("text,value,expected", [
    ("Inflation fell to 6.4% in March 2024.", 6.4, 1),
    ("Inflation fell to 6.4% in March 2024.", 6.43, 1),        # stated rounded
    ("It fell by 7.8 points.", -7.799999999999999, 1),          # sign in prose, float noise
    ("Output reached 12,345.67 million lei.", 12345.674, 2),
    ("Output reached 12,345 million lei.", 12345.2, 0),         # thousands grouping
    ("Rata a fost 5,1%.", 5.1, 1),                              # decimal comma
    ("The rate was 2.00%.", 2.0, 2),
    ("No number here.", 3.3, None),
    ("In 2024 the rate rose.", 6.4, None),                      # a year is not the value
])
def test_claim_decimals(text, value, expected):
    """The precision is the one the text uses to state the value."""
    assert claim_decimals(_claim(text, value)) == expected


@pytest.mark.parametrize("value,decimals,expected", [
    (2.25, 1, "2.3"), (6.45, 1, "6.5"), (0.125, 2, "0.13"), (6.55, 1, "6.6"), (-2.25, 1, "-2.3"),
    (1234567.891, 2, "1,234,567.89"), (-0.04, 1, "0.0"), (-7.8, 1, "-7.8"), (2.0, 0, "2"),
])
def test_format_number(value, decimals, expected):
    """Half-up rounding (ties away from zero, as the verifier, D-010) of the shortest decimal representation,
    grouped thousands, no negative zero."""
    assert format_number(value, decimals) == expected


def test_metric_decimals_from_claim_else_default():
    """A metric shows its stating claim's precision; unstated metrics use the default."""
    claims = [_claim("down 7.85 points", -7.85, metric_id="m1")]
    assert metric_decimals("m1", claims, 3) == 2
    assert metric_decimals("m2", claims, 3) == 3


def test_options_defaults_follow_settings():
    """ExportOptions defaults equal the Settings defaults and can be taken from settings."""
    assert ExportOptions().default_decimals == Settings.model_fields["export_default_decimals"].default
    s = Settings(database_url="postgresql://x/y", session_secret="s" * 32, export_chart_max_series=3,
                 _env_file=None)
    assert ExportOptions.from_settings(s).chart_max_series == 3


def test_locators():
    """Uploads are located by sheet/row/column, API observations by query URL."""
    b = ReportBundle.example()
    up = next(o for o in b.observations if o.provenance.source_type == "upload")
    api = next(o for o in b.observations if o.provenance.source_type == "api")
    assert observation_locator(up) == f"sheet Preturi, row {up.provenance.row_number}, column valoare"
    assert observation_locator(api).startswith("https://ec.europa.eu/eurostat/")
