"""Tests for period parsing."""
from datetime import date, datetime

import pytest

from statbrief.ingest.periods import PeriodError, infer_day_order, looks_like_excel_serial, parse_period


@pytest.mark.parametrize("raw,expected", [
    ("2024", ("2024", "A")), (2024, ("2024", "A")), (2024.0, ("2024", "A")),
    ("2024-Q1", ("2024-Q1", "Q")), ("2024Q3", ("2024-Q3", "Q")), ("Q2 2024", ("2024-Q2", "Q")),
    ("2024 Q4", ("2024-Q4", "Q")), ("q1-2024", ("2024-Q1", "Q")),
    ("2024M03", ("2024-03", "M")), ("2024-03", ("2024-03", "M")), ("2024/3", ("2024-03", "M")),
    ("03/2024", ("2024-03", "M")), ("3.2024", ("2024-03", "M")), ("2024-03-15", ("2024-03", "M")),
    ("Mar 2024", ("2024-03", "M")), ("martie 2024", ("2024-03", "M")), ("Ian. 2024", ("2024-01", "M")),
    (datetime(2024, 3, 1), ("2024-03", "M")), (date(2024, 12, 31), ("2024-12", "M")),
    ("25/03/2024", ("2024-03", "M")), ("03/25/2024", ("2024-03", "M")), ("05/05/2024", ("2024-05", "M")),
])
def test_parse_period(raw, expected):
    """Supported formats map to canonical periods."""
    p = parse_period(raw)
    assert (p.period, p.frequency) == expected


def test_ambiguous_day_month_needs_day_order():
    """01/02/2024 is ambiguous without a day order; resolved with one."""
    with pytest.raises(PeriodError) as exc:
        parse_period("01/02/2024")
    assert exc.value.code == "ambiguous_date"
    assert parse_period("01/02/2024", day_order="DMY").period == "2024-02"
    assert parse_period("01/02/2024", day_order="MDY").period == "2024-01"


@pytest.mark.parametrize("raw", ["2024-13", "2024M13", "32/01/2024", "", None, "hello", "2024-Q5", "12345"])
def test_invalid(raw):
    """Garbage and out-of-range values are invalid_date."""
    with pytest.raises(PeriodError) as exc:
        parse_period(raw)
    assert exc.value.code == "invalid_date"


def test_excel_serial_only_when_enabled():
    """45352 is 2024-03-01 as an Excel serial; without the flag it is invalid."""
    assert parse_period(45352, excel_serial=True).period == "2024-03"
    with pytest.raises(PeriodError):
        parse_period(45352)


def test_infer_day_order_and_serial_detection():
    """Day order is inferred from unambiguous values; serial columns are recognised."""
    assert infer_day_order(["01/02/2024", "25/02/2024"]) == "DMY"
    assert infer_day_order(["02/25/2024"]) == "MDY"
    assert infer_day_order(["01/02/2024"]) is None
    assert looks_like_excel_serial([45292, 45323, 45352], min_share=0.8) is True
    assert looks_like_excel_serial([2023, 2024], min_share=0.8) is False


@pytest.mark.parametrize("raw", ["1000-Q1", "Q2 9999", "1799", "2201M01"])
def test_out_of_range_years_invalid(raw):
    """Years outside the supported range are invalid for every frequency, quarters included."""
    with pytest.raises(PeriodError) as exc:
        parse_period(raw)
    assert exc.value.code == "invalid_date"


def test_infer_day_order_conflicting_evidence_is_undecided():
    """A column with both a DMY-only and an MDY-only value does not pick one (never guess)."""
    assert infer_day_order(["25/02/2024", "02/25/2024"]) is None
    assert infer_day_order([None, 45292, "13/01/2024", "01/02/2024"]) == "DMY"


def test_explicit_order_never_overrides_unambiguous_value():
    """An unambiguous value parses the same whatever day order is set."""
    assert parse_period("25/03/2024", day_order="MDY").period == "2024-03"
    assert parse_period("03/25/2024", day_order="DMY").period == "2024-03"


def test_fractional_excel_serial_is_floored_only_when_enabled():
    """A serial with a time of day maps to its date's month; detection stays integer-only."""
    assert parse_period(45352.75, excel_serial=True).period == "2024-03"
    assert parse_period("45352", excel_serial=True).period == "2024-03"
    with pytest.raises(PeriodError):
        parse_period(45352.75)
    with pytest.raises(PeriodError):
        parse_period("٤٥٣٥٢", excel_serial=True)  # non-ASCII digits are not serials
    assert looks_like_excel_serial([45352.5, 45353.5], min_share=0.5) is False


@pytest.mark.parametrize("raw,expected", [
    ("2024-03-15T00:00", "2024-03"), ("2024-03-15 13:45:10", "2024-03"), ("2024-03-15T08:30:00.123", "2024-03"),
    ("Iun 2024", "2024-06"), ("Iul. 2024", "2024-07"), ("Noi 2024", "2024-11"), ("15-03-2024", "2024-03"),
])
def test_iso_times_and_ins_abbreviations(raw, expected):
    """ISO date-times and Romanian INS month abbreviations parse."""
    assert parse_period(raw).period == expected


@pytest.mark.parametrize("raw", ["2024-03-15 garbage", "2024-03-15T", "01/02.2024", "25.03/2024"])
def test_malformed_iso_suffix_and_mixed_separators_invalid(raw):
    """Trailing junk after an ISO date and mixed d/m/y separators are invalid."""
    with pytest.raises(PeriodError) as exc:
        parse_period(raw, day_order="DMY")
    assert exc.value.code == "invalid_date"


def test_fractional_serial_strings():
    """ASCII decimal strings in the serial range are floored; other dotted strings parse as usual."""
    assert parse_period("45352.5", excel_serial=True).period == "2024-03"
    assert parse_period("2024.03", excel_serial=True).period == "2024-03"
    for raw in ("99999.5", "45352.5"):
        with pytest.raises(PeriodError) as exc:
            parse_period(raw, excel_serial=raw == "99999.5")
        assert exc.value.code == "invalid_date"
