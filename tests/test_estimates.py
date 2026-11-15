import tempfile
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.estimates import (
    MIB,
    Speed,
    bwlimit_bytes_per_second,
    choose_speed,
    estimate_seconds,
    format_estimate,
    load_expected_speed,
    observed_speed,
    save_expected_speed,
)
from app.db import Base


def test_format_estimate_rounds_to_readable_steps():
    assert format_estimate(30) == "under a minute"
    assert format_estimate(61) == "about 2 min"
    assert format_estimate(59 * 60) == "about 59 min"
    assert format_estimate(11 * 3600 + 38 * 60) == "about 11 h 40 min"
    assert format_estimate(2 * 3600) == "about 2 h"
    assert format_estimate(24 * 3600) == "about 1 day"
    assert format_estimate(3 * 86400 + 4 * 3600) == "about 3 days 4 h"


def test_observed_speed_uses_only_sizeable_runs_newest_first():
    big = 1024 * MIB
    samples = [
        (big, 100),  # 10.24 MiB/s
        (10 * MIB, 100),  # too little data
        (big, 30),  # too short
        (big, 60),  # 17.07 MiB/s
        (big, 200),  # 5.12 MiB/s
    ]
    rate, count = observed_speed(samples)
    assert count == 3
    assert rate == big / 100  # the median
    assert observed_speed([(10 * MIB, 100)]) is None
    assert observed_speed([(big, 100)] * 30)[1] == 10


def test_bwlimit_values_in_bytes_per_second():
    assert bwlimit_bytes_per_second("10M") == 10 * MIB
    assert bwlimit_bytes_per_second("3M:off") == 3 * MIB
    assert bwlimit_bytes_per_second("512") == 512 * 1024
    assert bwlimit_bytes_per_second("off") is None
    assert bwlimit_bytes_per_second(None) is None


def test_choose_speed_prefers_history_then_settings_and_respects_bwlimit():
    history = (40 * MIB, 4)
    assert choose_speed(history, 50, None).bytes_per_second == 40 * MIB
    assert "last 4 sizeable runs" in choose_speed(history, 50, None).explanation
    assert choose_speed(None, 50, None).explanation == "the expected upload speed in Settings"
    assert choose_speed(None, None, 2 * MIB).explanation == "the job's --bwlimit"
    capped = choose_speed(history, None, 2 * MIB)
    assert capped.bytes_per_second == 2 * MIB
    assert "lower than the median" in capped.explanation
    assert choose_speed(None, None, None) is None


def test_estimate_for_an_initial_sync():
    speed = Speed(50 * MIB, "test")
    # 2 TiB at 50 MiB/s is about 11.65 hours.
    assert format_estimate(estimate_seconds(2 * 1024**4, speed)) == "about 11 h 40 min"
    assert speed.label == "50.0 MiB/s"


def test_expected_speed_setting_round_trip():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = create_engine(f"sqlite:///{Path(tmpdir) / 'runs.db'}")
        Base.metadata.create_all(bind=engine)
        session_factory = sessionmaker(bind=engine)
        with session_factory() as db:
            assert load_expected_speed(db) is None
            save_expected_speed(db, 48.5)
            assert load_expected_speed(db) == 48.5
            save_expected_speed(db, 12)
            assert load_expected_speed(db) == 12
            save_expected_speed(db, None)
            assert load_expected_speed(db) is None
