import json
from html.parser import HTMLParser

from app.db import JobRecord, JobStepRecord
from app.main import _command_previews, templates


def test_command_preview_uses_common_args_and_job_environment():
    job = JobRecord(
        name="backup",
        cron="",
        common_args="--fast-list --bwlimit ${BW_LIMIT}",
        env_json=json.dumps({"BW_LIMIT": "8M"}),
    )
    job.steps = [
        JobStepRecord(position=1, name="Music", command="sync /media/Musiikki secret:/Musiikki")
    ]

    previews = _command_previews(job)

    assert previews == [
        {
            "id": None,
            "name": "Music",
            "command": "rclone sync --fast-list --bwlimit 8M /media/Musiikki secret:/Musiikki",
            "tokens": [
                "rclone",
                "sync",
                "--fast-list",
                "--bwlimit",
                "8M",
                "/media/Musiikki",
                "secret:/Musiikki",
            ],
        }
    ]


def test_step_run_buttons_do_not_create_nested_forms():
    job = JobRecord(id=1, name="backup", cron="", common_args="", env_json="{}")
    command_previews = [
        {
            "id": 2,
            "name": "Music",
            "command": "rclone sync /media/Musiikki secret:/Musiikki",
            "tokens": ["rclone", "sync", "/media/Musiikki", "secret:/Musiikki"],
        }
    ]

    html = templates.get_template("_job_editor.html").render(
        job=job,
        schedule_summary="Never",
        env_lines="",
        steps_text="Music|sync /media/Musiikki secret:/Musiikki",
        command_previews=command_previews,
    )

    parser = NestedFormParser()
    parser.feed(html)

    assert not parser.has_nested_form
    assert 'form="run-step-2"' in html
    assert 'form="dry-run-step-2"' in html
    assert 'id="run-step-2" method="post" action="/jobs/1/steps/2/run"' in html
    assert 'id="dry-run-step-2" method="post" action="/jobs/1/steps/2/dry-run"' in html


class NestedFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.form_depth = 0
        self.has_nested_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "form":
            if self.form_depth:
                self.has_nested_form = True
            self.form_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.form_depth -= 1
