"""One deployment configuration per service, and no shared root config.

The bug these pin: a single `railway.json` at the repository root is applied to
every service that has not been told otherwise. That file named the API's
Dockerfile and the API's start command, so `web` and `worker` would each have
been built and started as a second copy of the API — a failure that looks like
a working deploy until you notice the queue is never drained.

The other half is subtler. Railway's config-as-code has no field for a Docker
build target, so every service rooted at `apps/api` builds that Dockerfile's
default final stage (`api`). The start command is therefore the *only* thing
distinguishing the worker service from the API service, which is why it lives
in version control and is asserted here.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from ledgerai import cli

API_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = API_ROOT.parents[1]
RAILWAY_DIR = REPO_ROOT / "railway"

SERVICES = ("api", "worker", "web", "cron-demo-cleanup", "cron-retention")

# Services whose image is built from apps/api, and which therefore all receive
# the `api` stage. Their start commands must differ.
API_ROOTED = ("api", "worker", "cron-demo-cleanup", "cron-retention")


def load(service: str) -> dict:
    return json.loads((RAILWAY_DIR / f"{service}.json").read_text())


class TestNoSharedRootConfig:
    def test_there_is_no_railway_config_at_the_repository_root(self) -> None:
        """The regression itself: a root config silently applies to everything."""
        assert not (REPO_ROOT / "railway.json").exists()
        assert not (REPO_ROOT / "railway.toml").exists()

    def test_every_service_has_its_own_file(self) -> None:
        assert {path.stem for path in RAILWAY_DIR.glob("*.json")} == set(SERVICES)


class TestEveryConfigIsWellFormed:
    @pytest.mark.parametrize("service", SERVICES)
    def test_it_parses_and_builds_from_a_dockerfile(self, service: str) -> None:
        config = load(service)
        assert config["build"]["builder"] == "DOCKERFILE"
        # Relative to the service's Root Directory, which IS the build context.
        # `apps/api/Dockerfile` here would resolve to apps/api/apps/api/...
        assert config["build"]["dockerfilePath"] == "Dockerfile"

    @pytest.mark.parametrize("service", SERVICES)
    def test_it_declares_a_start_command(self, service: str) -> None:
        assert load(service)["deploy"]["startCommand"].strip()


class TestServicesCannotStartAsEachOther:
    def test_the_api_serves_http(self) -> None:
        deploy = load("api")["deploy"]
        assert "uvicorn ledgerai.main:app" in deploy["startCommand"]
        assert "$PORT" in deploy["startCommand"]
        # The access log records query strings, which carry merchant searches.
        assert "--no-access-log" in deploy["startCommand"]

    def test_the_worker_consumes_the_queue_and_is_not_a_second_api(self) -> None:
        command = load("worker")["deploy"]["startCommand"]
        assert command.startswith("rq worker ledgerai")
        assert "uvicorn" not in command

    def test_the_web_service_runs_the_next_server(self) -> None:
        command = load("web")["deploy"]["startCommand"]
        assert command == "node server.js"
        assert "uvicorn" not in command and "rq worker" not in command

    def test_every_api_rooted_service_has_a_distinct_command(self) -> None:
        """They all get the same image; only the command separates them."""
        commands = [load(service)["deploy"]["startCommand"] for service in API_ROOTED]
        assert len(set(commands)) == len(commands)


class TestMigrationsRunInExactlyOnePlace:
    def test_the_api_applies_them_before_it_starts(self) -> None:
        assert load("api")["deploy"]["preDeployCommand"] == "alembic upgrade head"

    @pytest.mark.parametrize(
        "service", [s for s in SERVICES if s != "api"]
    )
    def test_nothing_else_applies_them(self, service: str) -> None:
        """Concurrent `alembic upgrade head` is how a schema half-applies."""
        assert "preDeployCommand" not in load(service)["deploy"]


class TestHealthchecks:
    def test_the_api_is_probed_on_readiness_not_liveness(self) -> None:
        """/health stays 200 while degraded; probing it would never drain."""
        assert load("api")["deploy"]["healthcheckPath"] == "/health/ready"

    def test_the_worker_has_no_http_healthcheck(self) -> None:
        assert "healthcheckPath" not in load("worker")["deploy"]

    @pytest.mark.parametrize("service", ("cron-demo-cleanup", "cron-retention"))
    def test_cron_services_have_no_healthcheck(self, service: str) -> None:
        """They exit by design; a healthcheck would call that a failure."""
        assert "healthcheckPath" not in load(service)["deploy"]


class TestScheduledSweeps:
    @pytest.mark.parametrize(
        ("service", "schedule"),
        [("cron-demo-cleanup", "0 * * * *"), ("cron-retention", "0 4 * * *")],
    )
    def test_each_sweep_is_scheduled(self, service: str, schedule: str) -> None:
        assert load(service)["deploy"]["cronSchedule"] == schedule

    def test_demo_cleanup_runs_more_often_than_retention(self) -> None:
        """Demo accounts expire on a 24-hour clock; a daily sweep is too slow."""
        assert load("cron-demo-cleanup")["deploy"]["cronSchedule"].endswith("* * * *")

    @pytest.mark.parametrize("service", ("cron-demo-cleanup", "cron-retention"))
    def test_a_failed_sweep_does_not_restart_loop(self, service: str) -> None:
        assert load(service)["deploy"]["restartPolicyType"] == "NEVER"

    @pytest.mark.parametrize("service", ("cron-demo-cleanup", "cron-retention"))
    def test_the_scheduled_command_actually_exists_in_the_image(
        self, service: str
    ) -> None:
        """The original bug, asserted from the deployment side.

        A scheduled command that is not an installed console script is a run
        that fails at 04:00 with "not found" and is noticed weeks later.
        """
        command = load(service)["deploy"]["startCommand"]
        pyproject = tomllib.loads((API_ROOT / "pyproject.toml").read_text())
        assert command in pyproject["project"]["scripts"]

        # ...and it resolves to a command the dispatcher knows about.
        target = pyproject["project"]["scripts"][command]
        assert target.startswith("ledgerai.cli:")
        assert target.partition(":")[2] in {
            function.__name__ for function in cli.COMMANDS.values()
        }

    def test_no_sweep_is_invoked_through_the_repo_scripts_directory(self) -> None:
        """`scripts/` is not in the build context and never will be."""
        for service in SERVICES:
            assert "scripts/" not in load(service)["deploy"]["startCommand"]
