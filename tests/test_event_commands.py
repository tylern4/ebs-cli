import json

from conftest import EVENT_ID, TRIGGER_ID, FakeAPI, event_data
from typer.testing import CliRunner

from ebs_cli.cli import app


def test_event_list_sends_status_source_limit_and_cursor(runner: CliRunner, fake_api: FakeAPI) -> None:
    result = runner.invoke(
        app,
        [
            "events",
            "list",
            str(TRIGGER_ID),
            "--limit",
            "2",
            "--cursor",
            "opaque",
            "--status",
            "failed",
            "--source",
            "s3",
        ],
        env={"COLUMNS": "220"},
    )

    assert result.exit_code == 0, result.output
    assert fake_api.calls == [
        (
            "list_events",
            TRIGGER_ID,
            {"limit": 2, "cursor": "opaque", "status": "failed", "source": "s3"},
        )
    ]
    assert "next-page" in result.stdout
    assert "event-payload-not-shown" not in result.stdout
    assert "do-not-render" not in result.stdout
    assert "[bold]unsafe markup[/bold]" in result.stdout


def test_event_json_is_bounded_and_omits_unbounded_fields(runner: CliRunner, fake_api: FakeAPI) -> None:
    event = event_data()
    event["payload"] = {"huge": "not-rendered"}
    event["job"]["rendered_script"] = "not-rendered"
    event["job"]["error"] = "first\nsecond" + "x" * 1000
    fake_api.event_page.items = [type(fake_api.event_page.items[0]).model_validate(event)]

    result = runner.invoke(app, ["events", "list", str(TRIGGER_ID), "--json"])

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["next_cursor"] == "next-page"
    item = body["items"][0]
    assert item["id"] == str(EVENT_ID)
    assert "payload" not in item
    assert "rendered_script" not in item["job"]
    assert len(item["job"]["error"]) <= 500
    assert "\n" not in item["job"]["error"]
    assert "not-rendered" not in result.stdout


def test_event_limit_is_bounded_by_typer(runner: CliRunner, fake_api: FakeAPI) -> None:
    result = runner.invoke(app, ["events", "list", str(TRIGGER_ID), "--limit", "201"])

    assert result.exit_code == 2
    assert fake_api.calls == []
