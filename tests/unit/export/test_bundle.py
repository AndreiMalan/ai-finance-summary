"""ReportBundle contract: traceability checks and citation grouping."""
from datetime import datetime

import pytest
from pydantic import ValidationError

from statbrief.export.bundle import CITATION_GROUPS, ReportBundle


def _data(**changes) -> dict:
    """The example bundle as a dict with some fields replaced."""
    data = ReportBundle.example().model_dump()
    data.update(changes)
    return data


def test_example_is_valid_and_complete():
    """The example carries every kind of data-quality record and all three citation groups."""
    b = ReportBundle.example()
    assert b.metrics and b.observations and b.conflicts and b.validation_exclusions and b.unit_mismatches
    assert b.gap_fill is not None and b.gap_fill.filled
    assert [h for h, cites in b.citations_by_group() if cites] == [h for _, h in CITATION_GROUPS]


def test_claim_with_unknown_observation_rejected():
    """A claim citing an observation the bundle does not carry cannot be exported (no untraceable number)."""
    data = _data()
    data["draft"]["claims"][0]["observation_ids"] = ["obs_missing"]
    with pytest.raises(ValidationError, match="cites observations not in the bundle"):
        ReportBundle.model_validate(data)


def test_claim_with_unknown_metric_rejected():
    """A claim naming a metric the bundle does not carry is rejected."""
    with pytest.raises(ValidationError, match="names metric"):
        ReportBundle.model_validate(_data(metrics=[]))


def test_metric_with_unknown_input_rejected():
    """A metric whose inputs are not carried is rejected."""
    data = _data()
    data["metrics"][1]["observation_ids"] = ["obs_missing"]
    with pytest.raises(ValidationError, match="uses observations not in the bundle"):
        ReportBundle.model_validate(data)


def test_unresolved_citation_rejected():
    """Every draft citation needs a resolved source label."""
    data = _data()
    data["citations"] = data["citations"][:2]
    with pytest.raises(ValidationError, match="without a resolved source"):
        ReportBundle.model_validate(data)


def test_duplicate_observation_rejected():
    """Observation ids are unique inside a bundle."""
    data = _data()
    data["observations"] = data["observations"] + data["observations"][:1]
    with pytest.raises(ValidationError, match="duplicate obs_id"):
        ReportBundle.model_validate(data)


def test_resolution_must_match_its_conflict():
    """A conflict note pairs a conflict with its own resolution."""
    data = _data()
    data["conflicts"][0]["resolution"]["conflict_id"] = "cf_other"
    with pytest.raises(ValidationError, match="does not belong"):
        ReportBundle.model_validate(data)


def test_empty_citation_group_is_still_listed():
    """citations_by_group keeps empty groups so exports always print all three headings."""
    data = _data()
    data["draft"]["citations"] = [c for c in data["draft"]["citations"] if c["kind"] != "reference"]
    data["citations"] = [c for c in data["citations"] if c["kind"] != "reference"]
    groups = ReportBundle.model_validate(data).citations_by_group()
    assert [h for h, _ in groups] == ["Uploaded files", "Public APIs", "Reference documents"]
    assert groups[2][1] == []


def test_naive_timestamps_rejected():
    """Approval and generation times must carry a timezone."""
    with pytest.raises(ValidationError):
        ReportBundle.model_validate(_data(approved_at=datetime(2025, 1, 1)))
