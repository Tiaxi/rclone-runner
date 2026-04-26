from app.core.schedule import cron_summary, normalize_cron


def test_empty_cron_is_never():
    assert normalize_cron("  ") == ""
    assert cron_summary("  ") == "Never"


def test_common_cron_summaries_are_humanized():
    assert cron_summary("0 2 * * *") == "Daily at 02:00"
    assert cron_summary("30 3 * * 1") == "Weekly on Monday at 03:30"
    assert cron_summary("0 4 1 * *") == "Monthly on day 1 at 04:00"
    assert cron_summary("*/15 * * * *") == "Every 15 minutes"
    assert cron_summary("0 */6 * * *") == "Every 6 hours"
