import pytest
from pydantic import ValidationError

from app.schemas import JobCreate


def test_job_source_is_trimmed() -> None:
    payload = JobCreate(source_type="url", source="  https://b23.tv/abc  ")
    assert payload.source == "https://b23.tv/abc"


def test_empty_job_source_is_rejected() -> None:
    with pytest.raises(ValidationError):
        JobCreate(source_type="url", source="   ")

