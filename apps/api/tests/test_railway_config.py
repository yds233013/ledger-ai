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

API_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = API_ROOT.parents[1]
RAILWAY_DIR = REPO_ROOT / "railway"

SERVICES = ("api", "worker", "web")

# Services whose image is built from apps/api, and which therefore both receive
# the `api` stage. Their start commands must differ.
API_ROOTED = ("api", "worker")


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
        # The access log records query strings, which carry merchant searches.
        assert "--no-access-log" in deploy["startCommand"]

    def test_the_port_is_expanded_by_a_shell(self) -> None:
        """The bug this pins, observed on the first Railway deploy:

            Error: Invalid value for '--port': '$PORT' is not a valid integer.

        Railway execs the start command directly rather than handing it to a
        shell, so a bare `--port $PORT` reaches uvicorn as the four literal
        characters and it refuses to start. Wrapping the command in `sh -c` is
        what performs the expansion. The Dockerfile's own CMD already does
        this; the Railway config overrides CMD, so it has to do it too.
        """
        command = load("api")["deploy"]["startCommand"]
        assert command.startswith("sh -c ")
        assert "${PORT" in command
        # The exact form that failed. A default keeps the command runnable
        # anywhere PORT is not injected.
        assert "--port $PORT" not in command

    def test_uvicorn_remains_pid_1(self) -> None:
        """`exec` replaces the shell rather than forking under it.

        Without it the shell is PID 1, receives SIGTERM on a rolling deploy and
        exits without passing the signal on, so uvicorn is killed outright
        instead of draining — which cuts an in-progress SSE stream.
        """
        assert "exec uvicorn" in load("api")["deploy"]["startCommand"]

    def test_the_worker_runs_the_combined_entry_point(self) -> None:
        """`ledgerai-worker` is the RQ consumer plus the maintenance scheduler.

        It replaced a bare `rq worker`, which consumed the queue perfectly well
        but could not run the periodic sweeps. Those lost their own Railway
        services to the five-service Hobby cap, so the schedule moved into this
        process — see ledgerai/worker.py.
        """
        command = load("worker")["deploy"]["startCommand"]
        assert command == "ledgerai-worker"
        assert "uvicorn" not in command

    def test_the_worker_command_needs_no_shell_expansion(self) -> None:
        """Why this one is not wrapped in `sh -c`, unlike the API's.

        Railway execs the start command directly, so anything containing a
        variable has to be wrapped. This command takes no arguments and names no
        variable — the queue and Redis URL are read from the environment inside
        the process — so there is nothing to expand.
        """
        command = load("worker")["deploy"]["startCommand"]
        assert "$" not in command
        assert "sh -c" not in command

    def test_the_worker_entry_point_is_an_installed_console_script(self) -> None:
        """A start command that is not on PATH is a crash loop."""
        pyproject = tomllib.loads((API_ROOT / "pyproject.toml").read_text())
        command = load("worker")["deploy"]["startCommand"]
        assert command in pyproject["project"]["scripts"]
        assert pyproject["project"]["scripts"][command] == "ledgerai.worker:main"

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

class TestMaintenanceHasNoServiceOfItsOwn:
    """The sweeps moved into the worker; their config files are gone.

    Railway's Hobby plan caps a project at five services, which Postgres, Redis,
    api, worker and web fill exactly. Leaving the cron configs behind would
    describe a deployment that does not exist and cannot exist on this plan.
    """

    def test_the_cron_config_files_are_gone(self) -> None:
        assert not (RAILWAY_DIR / "cron-demo-cleanup.json").exists()
        assert not (RAILWAY_DIR / "cron-retention.json").exists()

    def test_no_config_declares_a_cron_schedule(self) -> None:
        for service in SERVICES:
            assert "cronSchedule" not in load(service)["deploy"]

    def test_the_worker_is_what_schedules_them(self) -> None:
        """The start command is the load-bearing part: `rq worker` would drain
        the queue but never sweep."""
        assert load("worker")["deploy"]["startCommand"] == "ledgerai-worker"

        from ledgerai.maintenance import default_sweeps

        assert {s.name for s in default_sweeps()} == {
            "demo-cleanup",
            "retention",
            "account-reconcile",
        }

    def test_the_sweeps_stay_invocable_by_hand(self) -> None:
        """`railway run --service worker ledgerai-demo-cleanup` must still work."""
        pyproject = tomllib.loads((API_ROOT / "pyproject.toml").read_text())
        scripts = pyproject["project"]["scripts"]
        assert "ledgerai-demo-cleanup" in scripts
        assert "ledgerai-retention-sweep" in scripts

    def test_no_service_is_invoked_through_the_repo_scripts_directory(self) -> None:
        """`scripts/` is not in the build context and never will be."""
        for service in SERVICES:
            assert "scripts/" not in load(service)["deploy"]["startCommand"]
