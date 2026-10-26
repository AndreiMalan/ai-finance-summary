"""Tests for numeric cell parsing."""
import pytest

from statbrief.ingest.numbers import NumberError, infer_decimal, parse_number


@pytest.mark.parametrize("raw,decimal,expected", [
    ("7,4", None, 7.4), ("7.4", None, 7.4), ("1.234,5", None, 1234.5), ("1,234.5", None, 1234.5),
    ("1 234,5", ",", 1234.5), ("1 234", None, 1234.0), ("1,234,567", None, 1234567.0),
    ("6.6%", None, 6.6), ("-0,3", None, -0.3), (3, None, 3.0), (2.5, None, 2.5),
    ("1.234", ",", 1234.0),  # brief case ("1,5", ".", 15.0) moved to malformed groups (fix round 1)
])
def test_parse_number(raw, decimal, expected):
    """Common separators and formats parse correctly."""
    assert parse_number(raw, decimal) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [None, "", " ", "-", ":", "..", "n/a", "NA", "null", float("nan")])
def test_missing_tokens(raw):
    """Missing markers become None (a gap), never 0."""
    assert parse_number(raw) is None


@pytest.mark.parametrize("raw", ["abc", "7,4,x", True, "1e", "=1+1"])
def test_non_numeric(raw):
    """Non-numeric content raises NumberError."""
    with pytest.raises(NumberError):
        parse_number(raw)


def test_infer_decimal():
    """The dominant decimal separator is inferred from sample values."""
    assert infer_decimal(["7,4", "7,1", "6,9"]) == ","
    assert infer_decimal(["7.4", "7.1"]) == "."
    assert infer_decimal(["7", "8"]) is None


@pytest.mark.parametrize("raw", [float("inf"), float("-inf"), "1e999", "-1e999", "inf"])
def test_non_finite_rejected(raw):
    """Infinities (given directly or overflowing from text) are errors, never values; only NaN is a gap."""
    with pytest.raises(NumberError):
        parse_number(raw)


@pytest.mark.parametrize("raw,decimal,expected", [
    ("1.234.567", None, 1234567.0), ("1.234.567,5", None, 1234567.5), ("1\u00a0234,5", None, 1234.5),
    ("1\u202f234", None, 1234.0), ("1'234.5", None, 1234.5), ("+5", None, 5.0), ("1e3", None, 1000.0),
])
def test_parse_number_more_formats(raw, decimal, expected):
    """Dotted thousands, non-breaking/thin-space and apostrophe separators, signs and exponents parse."""
    assert parse_number(raw, decimal) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["1_000", "infinity", "0x10", "1.2.3,4,5", 10**400, "%"])
def test_lenient_python_forms_rejected(raw):
    """Python-only literal forms and impossible separators are not numbers in a statistics file."""
    with pytest.raises(NumberError):
        parse_number(raw)


@pytest.mark.parametrize("raw,decimal", [
    ("15.03.2024", None), ("1.2.3", None), ("1.234.5", None), ("1,234,5", None), ("1,2,3", None),
    ("1,23.4", None), ("12,34,567.8", None), ("1,5", "."), ("1,234.5", ","), ("1.234,5,6", None),
])
def test_malformed_thousands_groups_rejected(raw, decimal):
    """Thousands separators must form 3-digit groups after a 1-3 digit head; anything else is not a number."""
    with pytest.raises(NumberError):
        parse_number(raw, decimal)


@pytest.mark.parametrize("raw,decimal,expected", [
    ("1,234,567", None, 1234567.0), ("1.234.567", None, 1234567.0), ("1.234.567,5", None, 1234567.5),
    ("1 234,5", ",", 1234.5), ("12.345,6", None, 12345.6), ("-1.234,5", None, -1234.5), ("1,234", None, 1.234),
])
def test_well_formed_thousands_groups_parse(raw, decimal, expected):
    """Proper thousands grouping still parses; a lone "1,234" without a decimal is 1.234 per cell."""
    assert parse_number(raw, decimal) == pytest.approx(expected)


@pytest.mark.parametrize("values,expected", [
    (["1,234", "2,345", "3,456"], None), (["1,234", "7,4"], ","), (["1.234", "7.4"], "."),
    (["-1.234", "1.234"], None),
])
def test_infer_decimal_ignores_thousands_lookalikes(values, expected):
    """Values with exactly three digits after one separator are not evidence for either separator."""
    assert infer_decimal(values) == expected


@pytest.mark.parametrize("values,expected", [
    (["1.234,5", "1.234"], ","), (["1,234.5", "1,234"], "."), (["1.234,5", "1,234.5"], None),
    (["1.23,4", "1.2.3,4"], None),
])
def test_infer_decimal_uses_well_grouped_both_separator_values(values, expected):
    """A well-grouped value with both separators is evidence for the one that occurs last."""
    assert infer_decimal(values) == expected
