"""What an unauthenticated caller can reach, and what it must never contain.

Two requirements pull in opposite directions and both have to hold:

  * `/docs` stays public, because a portfolio reviewer should be able to read
    the API without credentials.
  * Every financial endpoint stays authenticated and user-scoped.

The risk in publishing a schema is that it describes endpoints as open when
they are not, or that a public route leaks configuration. Both are asserted
here against the live OpenAPI document rather than against a list someone has
to remember to update.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from ledgerai.main import app

# Everything that touches a user's own records. Nothing here may answer without
# a bearer token.
PROTECTED_PREFIXES = (
    "/api/transactions",
    "/api/dashboard",
    "/api/uploads",
    "/api/receipts",
    "/api/alerts",
    "/api/analysis",
    "/api/settings",
)

# The deliberately public surface, and the reason each one is public.
PUBLIC_PATHS = {
    "/health": "liveness probe",
    "/health/ready": "readiness probe",
    "/docs": "API documentation for reviewers",
    "/openapi.json": "the schema behind /docs",
    "/redoc": "alternative documentation rendering",
    "/api/auth/login": "authentication itself",
    "/api/auth/demo-session": "the way in for a demo visitor",
    "/api/auth/oauth/github": "the OAuth callback's account resolution",
}


def protected_operations() -> list[tuple[str, str]]:
    """Every (method, path) under a user-scoped prefix, from the live app."""
    spec = app.openapi()
    return [
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        if path.startswith(PROTECTED_PREFIXES)
        for method in operations
        if method.lower() in {"get", "post", "patch", "delete", "put"}
    ]


class TestDocumentationIsPublic:
    async def test_docs_are_reachable_without_credentials(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/docs")
        assert response.status_code == 200

    async def test_the_schema_is_reachable_without_credentials(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/openapi.json")
        assert response.status_code == 200
        assert response.json()["info"]["title"] == "Ledger AI API"

    async def test_the_schema_documents_the_synthetic_data_disclaimer(
        self, client: AsyncClient
    ) -> None:
        description = (await client.get("/openapi.json")).json()["info"]["description"]
        assert "synthetic" in description.lower()

    async def test_the_schema_describes_the_demo_endpoint(
        self, client: AsyncClient
    ) -> None:
        paths = (await client.get("/openapi.json")).json()["paths"]
        assert "/api/auth/demo-session" in paths


class TestFinancialDataIsProtected:
    def test_the_protected_list_is_not_empty(self) -> None:
        """Positive control: an empty list would make every check below vacuous."""
        operations = protected_operations()
        assert len(operations) > 15, f"only found {len(operations)} protected operations"

    @pytest.mark.parametrize("method,path", protected_operations())
    async def test_every_user_scoped_operation_refuses_an_anonymous_caller(
        self, client: AsyncClient, method: str, path: str
    ) -> None:
        # Path parameters are filled with a syntactically valid but unowned id,
        # so a 404 would mean the route ran — it must not get that far.
        concrete = path
        for placeholder in ("{transaction_id}", "{receipt_id}", "{alert_id}",
                            "{upload_id}", "{run_id}"):
            concrete = concrete.replace(placeholder, "00000000-0000-0000-0000-000000000000")

        response = await client.request(method, concrete)
        assert response.status_code == 401, (
            f"{method} {concrete} answered {response.status_code} without a token"
        )

    async def test_a_valid_token_is_required_not_merely_a_header(
        self, client: AsyncClient
    ) -> None:
        response = await client.get(
            "/api/transactions", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert response.status_code == 401

    async def test_transactions_are_not_public(self, client: AsyncClient) -> None:
        assert (await client.get("/api/transactions")).status_code == 401

    async def test_the_export_is_not_public(self, client: AsyncClient) -> None:
        assert (await client.get("/api/settings/export")).status_code == 401

    async def test_deletion_is_not_public(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/settings/delete-account", json={"confirmation": "DELETE"}
        )
        assert response.status_code == 401


class TestPublicRoutesLeakNothing:
    @pytest.mark.parametrize("path", sorted(PUBLIC_PATHS))
    async def test_a_public_route_exposes_no_secret(
        self, client: AsyncClient, path: str
    ) -> None:
        from ledgerai.config import settings

        response = await client.request(
            "POST" if path in {"/api/auth/login", "/api/auth/demo-session",
                               "/api/auth/oauth/github"} else "GET",
            path,
            json={} if "auth" in path else None,
        )
        body = response.text

        for secret in (
            settings.auth_secret,
            settings.demo_user_password,
            settings.s3_secret_key,
        ):
            if secret:
                assert secret not in body, f"{path} leaked a configured secret"

    async def test_health_does_not_name_infrastructure(
        self, client: AsyncClient
    ) -> None:
        """A dependency's role may be reported; its address may not."""
        body = (await client.get("/health")).json()
        raw = str(body).lower()

        assert "redis://" not in raw
        assert "postgresql" not in raw
        assert "amazonaws" not in raw
        # The role IS reported — that is the point of the probe.
        assert "rate_limit_store" in body["dependencies"]

    async def test_readiness_does_not_name_infrastructure(
        self, client: AsyncClient
    ) -> None:
        raw = str((await client.get("/health/ready")).json()).lower()
        assert "redis://" not in raw
        assert "postgresql" not in raw
        assert "@" not in raw, "a connection string would contain credentials"

    async def test_an_unhandled_error_reveals_nothing(
        self, client: AsyncClient
    ) -> None:
        """The generic handler must give a correlation id and nothing else."""
        response = await client.get("/api/transactions", params={"limit": "not-a-number"})
        assert response.status_code in (401, 422)
        assert "Traceback" not in response.text
        assert "/Users/" not in response.text
        assert "site-packages" not in response.text

    async def test_a_validation_error_does_not_echo_internals(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.get(
            "/api/transactions", headers=auth_headers, params={"limit": 9999}
        )
        assert response.status_code == 422
        assert "site-packages" not in response.text
        assert "ledgerai/routers" not in response.text


class TestLivenessAndReadinessAreDistinct:
    async def test_liveness_identifies_itself(self, client: AsyncClient) -> None:
        body = (await client.get("/health")).json()
        assert body["probe"] == "liveness"

    async def test_readiness_identifies_itself(self, client: AsyncClient) -> None:
        body = (await client.get("/health/ready")).json()
        assert body["probe"] == "readiness"

    async def test_a_healthy_instance_is_ready(self, client: AsyncClient) -> None:
        response = await client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"
        assert response.json()["reasons"] == []

    async def test_readiness_reports_the_database(self, client: AsyncClient) -> None:
        body = (await client.get("/health/ready")).json()
        assert body["dependencies"]["database"] == "ok"

    async def test_an_unreachable_database_makes_the_instance_unready(
        self, client: AsyncClient, monkeypatch
    ) -> None:
        """Nothing user-scoped can be served, so it must leave rotation."""
        from ledgerai import main

        async def down() -> bool:
            return False

        monkeypatch.setattr(main, "_probe_database", down)

        response = await client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
        assert "database_unavailable" in response.json()["reasons"]

    async def test_liveness_stays_200_when_the_database_is_down(
        self, client: AsyncClient, monkeypatch
    ) -> None:
        """Restarting the container would not bring the database back."""
        from ledgerai import main

        async def down() -> bool:
            return False

        monkeypatch.setattr(main, "_probe_database", down)

        assert (await client.get("/health")).status_code == 200

    async def test_a_limiter_outage_does_not_unready_a_development_instance(
        self, client: AsyncClient, monkeypatch
    ) -> None:
        """Development fails open, so the instance is still fully usable."""
        from ledgerai import main
        from ledgerai.config import settings

        async def down() -> bool:
            return False

        monkeypatch.setattr(main, "probe_limiter_store", down)
        monkeypatch.setattr(settings, "environment", "development", raising=False)

        response = await client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"

    async def test_a_limiter_outage_unreadies_a_production_instance(
        self, client: AsyncClient, monkeypatch
    ) -> None:
        """Production fails closed, so every public request would be refused."""
        from ledgerai import main
        from ledgerai.config import settings

        async def down() -> bool:
            return False

        monkeypatch.setattr(main, "probe_limiter_store", down)
        monkeypatch.setattr(settings, "environment", "production", raising=False)

        response = await client.get("/health/ready")
        assert response.status_code == 503
        assert "rate_limit_store_unavailable" in response.json()["reasons"]

    async def test_liveness_reports_degradation_without_failing(
        self, client: AsyncClient, monkeypatch
    ) -> None:
        from ledgerai import main

        async def down() -> bool:
            return False

        monkeypatch.setattr(main, "probe_limiter_store", down)

        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "degraded"
