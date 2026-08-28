"""The scheduled sweeps must be invocable from inside the deployed image.

The bug these pin: the sweeps were documented as `python scripts/retention_sweep.py`,
but the image is built from the `apps/api` context and does not contain the
repository-root `scripts/` directory. Every scheduled run would have failed
with "No such file or directory" — and failed quietly, because nothing in the
product notices that expired demo accounts have stopped being deleted.

So the tests here are less about the CLI's own logic than about the join
between three things that have to agree: the functions, the console-script
entry points that make them reachable on PATH, and the start commands the
deployment configuration actually schedules.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from ledgerai import cli

API_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = API_ROOT.parents[1]


def _console_scripts() -> dict[str, str]:
    pyproject = tomllib.loads((API_ROOT / "pyproject.toml").read_text())
    return pyproject["project"].get("scripts", {})


class TestConsoleScriptsAreDefined:
    """`[project.scripts]` is what puts these commands on PATH in the image."""

    def test_both_sweeps_are_declared(self) -> None:
        """The sweeps stay individually invocable even though the worker now
        schedules them, because `railway run ledgerai-demo-cleanup` is how an
        operator forces one by hand."""
        assert {"ledgerai-demo-cleanup", "ledgerai-retention-sweep"} <= set(_console_scripts())

    def test_the_worker_entry_point_is_declared(self) -> None:
        assert _console_scripts()["ledgerai-worker"] == "ledgerai.worker:main"

    @pytest.mark.parametrize(
        ("script", "target"),
        [
            ("ledgerai-demo-cleanup", "ledgerai.cli:demo_cleanup_main"),
            ("ledgerai-retention-sweep", "ledgerai.cli:retention_sweep_main"),
        ],
    )
    def test_each_entry_point_resolves(self, script: str, target: str) -> None:
        assert _console_scripts()[script] == target

        module_name, _, attribute = target.partition(":")
        # A typo in the entry point is invisible until a cron run at 04:00, so
        # the string is resolved here the same way the installer resolves it.
        module = __import__(module_name, fromlist=[attribute])
        assert callable(getattr(module, attribute))

    def test_the_entry_points_are_the_functions_the_dispatcher_uses(self) -> None:
        """One implementation, reachable two ways — not two that can drift."""
        assert cli.COMMANDS["demo-cleanup"] is cli.demo_cleanup_main
        assert cli.COMMANDS["retention-sweep"] is cli.retention_sweep_main


class TestTheSweepsDoNotDependOnTheRepositoryLayout:
    def test_the_cli_module_ships_inside_the_package(self) -> None:
        """If this moves out of `ledgerai/`, it stops being in the image."""
        assert (API_ROOT / "ledgerai" / "cli.py").is_file()

    def test_the_image_copies_the_package_that_contains_it(self) -> None:
        dockerfile = (API_ROOT / "Dockerfile").read_text()
        assert "COPY ledgerai ./ledgerai" in dockerfile
        # The inverse of the original bug: nothing may reintroduce a dependency
        # on a directory that is outside the build context.
        assert "COPY ../../scripts" not in dockerfile

    def test_the_repo_scripts_delegate_rather_than_reimplement(self) -> None:
        """The local wrappers must call the same code the image runs."""
        for name, function in (
            ("demo_cleanup.py", "demo_cleanup_main"),
            ("retention_sweep.py", "retention_sweep_main"),
        ):
            source = (REPO_ROOT / "scripts" / name).read_text()
            assert f"from ledgerai.cli import {function}" in source


class TestDispatch:
    def test_a_known_command_runs_and_reports_success(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(
            "ledgerai.jobs.demo_cleanup.run_demo_cleanup",
            lambda: {"users_deleted": 3},
        )
        assert cli.main(["demo-cleanup"]) == 0
        assert json.loads(capsys.readouterr().out) == {"users_deleted": 3}

    def test_an_unknown_command_is_a_usage_error(self, capsys) -> None:
        assert cli.main(["not-a-command"]) == 2
        assert "usage:" in capsys.readouterr().err

    def test_no_command_is_a_usage_error(self) -> None:
        assert cli.main([]) == 2

    def test_extra_arguments_are_refused(self) -> None:
        assert cli.main(["demo-cleanup", "--force"]) == 2


class TestExitStatus:
    """A scheduler reads the exit code and nothing else."""

    def test_success_is_zero(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(
            "ledgerai.jobs.retention.run_retention_sweep",
            lambda: {"uploads_failed": 0},
        )
        assert cli.retention_sweep_main() == 0
        assert json.loads(capsys.readouterr().out) == {"uploads_failed": 0}

    def test_a_failing_sweep_exits_non_zero_instead_of_raising(
        self, monkeypatch, capsys
    ) -> None:
        """A traceback that exits 0 is a silently broken schedule."""

        def _explode() -> dict[str, object]:
            raise RuntimeError("database is on fire")

        monkeypatch.setattr("ledgerai.jobs.retention.run_retention_sweep", _explode)
        assert cli.retention_sweep_main() == 1
        # Nothing is printed to stdout, so a log scraper cannot mistake a
        # failure for an empty report.
        assert capsys.readouterr().out == ""

    def test_a_report_with_awkward_values_still_serializes(
        self, monkeypatch, capsys
    ) -> None:
        """Reports carry datetimes; `default=str` must keep them printable."""
        from datetime import datetime

        monkeypatch.setattr(
            "ledgerai.jobs.demo_cleanup.run_demo_cleanup",
            lambda: {"swept_at": datetime(2026, 8, 27, 4, 0, 0)},
        )
        assert cli.demo_cleanup_main() == 0
        assert "2026-08-27" in capsys.readouterr().out
