"""The CLI, driven through `main(argv)` so exit codes are part of the contract.

`--etcd` is passed explicitly rather than through the environment: the module
reads `CONFIGSTORE_ETCD_URL` once at import, so a test that set it afterwards
would be testing nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from configstore.admin import ConfigAdmin
from configstore.cli import _value, main
from configstore.schema import Schema

pytestmark = pytest.mark.etcd


# `main` with the endpoint and actor already supplied.
Run = Callable[..., int]


@pytest.fixture
def run(etcd_url: str) -> Run:
    def invoke(*argv: str) -> int:
        return main(["--etcd", etcd_url, "--actor", "cli-tests", *argv])

    return invoke


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [
        ("12", 12),
        ("true", True),
        ("0.92", 0.92),
        ("qwen/qwen3-vl-8b", "qwen/qwen3-vl-8b"),
    ],
)
def test_values_are_json_first_then_strings(raw: str, parsed: object) -> None:
    # So `--value 12` is an integer without making anyone quote a model id.
    assert _value(raw) == parsed


def test_status_reports_the_cluster(
    run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("status") == 0
    out = capsys.readouterr().out
    assert "revision" in out and "dbSize" in out


def test_set_get_and_list(
    run: Run, capsys: pytest.CaptureFixture[str], admin: ConfigAdmin, app: str
) -> None:
    assert (
        run(
            "set",
            "--app",
            app,
            "--layer",
            "prod",
            "--key",
            "retrieval.top_k",
            "--value",
            "12",
        )
        == 0
    )
    capsys.readouterr()
    assert run("get", "--app", app, "--layer", "prod", "--key", "retrieval.top_k") == 0
    assert capsys.readouterr().out.startswith("12")
    assert run("list", "--app", app, "--layer", "prod") == 0
    assert "retrieval.top_k" in capsys.readouterr().out


def test_a_missing_key_exits_nonzero(
    run: Run, capsys: pytest.CaptureFixture[str], admin: ConfigAdmin, app: str
) -> None:
    assert run("get", "--app", app, "--layer", "prod", "--key", "chat.model") == 1
    assert "absent" in capsys.readouterr().out


def test_a_conflict_has_its_own_exit_code(
    run: Run, capsys: pytest.CaptureFixture[str], admin: ConfigAdmin, app: str
) -> None:
    # 2 rather than 1, so a script can tell "someone else changed it" from
    # "that was a bad request" and retry only the former.
    run(
        "set",
        "--app",
        app,
        "--layer",
        "prod",
        "--key",
        "retrieval.top_k",
        "--value",
        "1",
    )
    capsys.readouterr()
    assert (
        run(
            "set",
            "--app",
            app,
            "--layer",
            "prod",
            "--key",
            "retrieval.top_k",
            "--value",
            "2",
            "--expect",
            "1",
        )
        == 2
    )
    assert "refused" in capsys.readouterr().err


def test_a_value_the_schema_rejects_exits_nonzero(
    run: Run, capsys: pytest.CaptureFixture[str], admin: ConfigAdmin, app: str
) -> None:
    assert (
        run(
            "set",
            "--app",
            app,
            "--layer",
            "prod",
            "--key",
            "retrieval.top_k",
            "--value",
            '"eight"',
        )
        == 1
    )
    assert "error" in capsys.readouterr().err


def test_resolve_prints_the_layer_each_value_came_from(
    run: Run, capsys: pytest.CaptureFixture[str], admin: ConfigAdmin, app: str
) -> None:
    admin.set(app, "base", "retrieval.top_k", 8)
    admin.set(app, "prod", "retrieval.top_k", 12)
    assert run("resolve", "--app", app, "--env", "prod") == 0
    out = capsys.readouterr().out
    assert "[prod]" in out and "[default]" in out  # overridden, and untouched


def test_history_and_rollback(
    run: Run, capsys: pytest.CaptureFixture[str], admin: ConfigAdmin, app: str
) -> None:
    admin.set(app, "prod", "retrieval.top_k", 1)
    admin.set(app, "prod", "retrieval.top_k", 2)
    assert (
        run("history", "--app", app, "--layer", "prod", "--key", "retrieval.top_k") == 0
    )
    assert "cli-tests" not in capsys.readouterr().out  # written by the fixture's actor
    assert (
        run(
            "rollback",
            "--app",
            app,
            "--layer",
            "prod",
            "--key",
            "retrieval.top_k",
            "--to",
            "1",
        )
        == 0
    )
    capsys.readouterr()
    assert admin.read_layer(app, "prod")["retrieval.top_k"].json() == 1


def test_rollback_to_a_revision_that_is_not_an_entry_fails(
    run: Run, capsys: pytest.CaptureFixture[str], admin: ConfigAdmin, app: str
) -> None:
    admin.set(app, "prod", "retrieval.top_k", 1)
    assert (
        run(
            "rollback",
            "--app",
            app,
            "--layer",
            "prod",
            "--key",
            "retrieval.top_k",
            "--to",
            "99",
        )
        == 1
    )
    assert "no history entry" in capsys.readouterr().err


def test_schema_publishes_and_prints(
    run: Run, capsys: pytest.CaptureFixture[str], tmp_path: Path, schema: Schema
) -> None:
    path = tmp_path / "schema.json"
    path.write_bytes(schema.to_bytes())
    assert run("push-schema", "--file", str(path)) == 0
    capsys.readouterr()
    assert run("schema", "--app", schema.app) == 0
    assert json.loads(capsys.readouterr().out)["app"] == schema.app


def test_unset_removes_and_reports(
    run: Run, capsys: pytest.CaptureFixture[str], admin: ConfigAdmin, app: str
) -> None:
    admin.set(app, "prod", "retrieval.top_k", 5)
    assert (
        run("unset", "--app", app, "--layer", "prod", "--key", "retrieval.top_k") == 0
    )
    assert "removed" in capsys.readouterr().out
    assert "retrieval.top_k" not in admin.read_layer(app, "prod")
