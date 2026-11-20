"""The settings of the files domain (LLD 6): `KYAPEHNU_FILES_BUCKET`, `KYAPEHNU_FILES_REGION`, `KYAPEHNU_SWEEP_MAX_DELETES`.
An empty variable means "not set" (compose passes `${VAR:-}`), a bad value refuses to start."""

import pytest
from pydantic import ValidationError

from kyapehnu.core.config import Settings


def _settings(**env: str) -> Settings:
    return Settings(_env_file=None, **env)


def test_the_defaults_are_the_laptop_the_mumbai_region_and_a_cap_of_500():
    s = _settings()

    assert (s.files_bucket, s.files_region, s.sweep_max_deletes) == (None, "ap-south-1", 500)


def test_the_three_variables_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("KYAPEHNU_FILES_BUCKET", "kyapehnu-pictures-123456789012")
    monkeypatch.setenv("KYAPEHNU_FILES_REGION", "eu-west-1")
    monkeypatch.setenv("KYAPEHNU_SWEEP_MAX_DELETES", "2000")

    s = _settings()

    assert (s.files_bucket, s.files_region, s.sweep_max_deletes) == (
        "kyapehnu-pictures-123456789012",
        "eu-west-1",
        2000,
    )


@pytest.mark.parametrize("name", ["KYAPEHNU_FILES_BUCKET", "KYAPEHNU_FILES_REGION", "KYAPEHNU_SWEEP_MAX_DELETES"])
def test_an_empty_variable_is_the_same_as_not_set(monkeypatch, name):
    monkeypatch.setenv(name, "")

    s = _settings()

    assert (s.files_bucket, s.files_region, s.sweep_max_deletes) == (None, "ap-south-1", 500)


@pytest.mark.parametrize(
    "bucket",
    ["abc", "test-pictures", "a.b-c", "0abc9", "kyapehnu-pictures-123456789012", "a" + "b" * 61 + "c"],
)
def test_a_valid_bucket_name_is_accepted(bucket):
    assert _settings(files_bucket=bucket).files_bucket == bucket


@pytest.mark.parametrize(
    "bucket",
    [
        "ab",
        "a" * 64,
        "Upper-Case",
        "-leading-dash",
        "trailing-dash-",
        ".leading-dot",
        "trailing-dot.",
        "has_underscore",
        "has space",
        "a/b",
        "s3://bucket",
        "bucket\n",
    ],
)
def test_a_bucket_name_s3_would_refuse_stops_the_start(bucket):
    with pytest.raises(ValidationError):
        _settings(files_bucket=bucket)


def test_a_bad_bucket_name_from_the_environment_stops_the_start(monkeypatch):
    monkeypatch.setenv("KYAPEHNU_FILES_BUCKET", "Not_A_Bucket")

    with pytest.raises(ValidationError):
        _settings()


@pytest.mark.parametrize("cap", [1, 500, 100000])
def test_the_sweep_cap_accepts_its_bounds(cap):
    assert _settings(sweep_max_deletes=cap).sweep_max_deletes == cap


@pytest.mark.parametrize("cap", [0, -1, 100001, "many"])
def test_the_sweep_cap_outside_1_to_100000_stops_the_start(cap):
    with pytest.raises(ValidationError):
        _settings(sweep_max_deletes=cap)
