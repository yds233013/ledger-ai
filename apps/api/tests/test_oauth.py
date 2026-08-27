"""GitHub identity resolution.

GitHub sign-in is optional, but where it exists the linking rule is the whole
security story: an account is found by the provider's immutable account id and
by nothing else. Adopting an existing account because the email matches — even
an email the provider says it verified — would let anyone who can set their
GitHub address to a known user's address inherit that user's financial data.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ledgerai.models import User


def payload(**overrides) -> dict:
    body = {
        "provider_account_id": "12345678",
        "email": "octocat@example.invalid",
        "email_verified": True,
        "display_name": "The Octocat",
    }
    body.update(overrides)
    return body


class TestNewIdentity:
    async def test_it_creates_an_account(self, client: AsyncClient) -> None:
        response = await client.post("/api/auth/oauth/github", json=payload())

        assert response.status_code == 200
        body = response.json()
        assert body["created"] is True
        assert body["user"]["display_name"] == "The Octocat"

    async def test_the_account_is_not_a_demo_account(self, client: AsyncClient) -> None:
        body = (await client.post("/api/auth/oauth/github", json=payload())).json()
        assert body["user"]["is_demo"] is False
        assert body["user"]["demo_expires_at"] is None

    async def test_a_verified_unused_address_is_kept(self, client: AsyncClient) -> None:
        body = (await client.post("/api/auth/oauth/github", json=payload())).json()
        assert body["user"]["email"] == "octocat@example.invalid"

    async def test_an_unverified_address_is_not_used_as_an_identifier(
        self, client: AsyncClient
    ) -> None:
        body = (
            await client.post(
                "/api/auth/oauth/github",
                json=payload(email="someone@example.invalid", email_verified=False),
            )
        ).json()

        assert body["user"]["email"] != "someone@example.invalid"
        assert body["user"]["email"].endswith("@github.ledgerai.invalid")

    async def test_a_missing_address_is_handled(self, client: AsyncClient) -> None:
        body = (
            await client.post(
                "/api/auth/oauth/github", json=payload(email=None, email_verified=False)
            )
        ).json()
        assert body["created"] is True
        assert body["user"]["email"].endswith("@github.ledgerai.invalid")

    async def test_a_missing_display_name_gets_a_fallback(
        self, client: AsyncClient
    ) -> None:
        body = (
            await client.post("/api/auth/oauth/github", json=payload(display_name=None))
        ).json()
        assert body["user"]["display_name"] == "GitHub user"


class TestReturningIdentity:
    async def test_the_same_github_account_resolves_to_the_same_user(
        self, client: AsyncClient
    ) -> None:
        first = (await client.post("/api/auth/oauth/github", json=payload())).json()
        second = (await client.post("/api/auth/oauth/github", json=payload())).json()

        assert second["user"]["id"] == first["user"]["id"]
        assert second["created"] is False

    async def test_repeat_sign_ins_do_not_multiply_accounts(
        self, client: AsyncClient, sync_db: Session
    ) -> None:
        for _ in range(3):
            await client.post("/api/auth/oauth/github", json=payload())

        count = sync_db.execute(
            select(func.count(User.id)).where(User.github_id == "12345678")
        ).scalar_one()
        assert count == 1

    async def test_a_changed_display_name_does_not_create_a_second_account(
        self, client: AsyncClient
    ) -> None:
        first = (await client.post("/api/auth/oauth/github", json=payload())).json()
        second = (
            await client.post(
                "/api/auth/oauth/github", json=payload(display_name="Renamed")
            )
        ).json()
        assert second["user"]["id"] == first["user"]["id"]

    async def test_a_changed_email_does_not_create_a_second_account(
        self, client: AsyncClient
    ) -> None:
        """The account id is the identity; the address is display data."""
        first = (await client.post("/api/auth/oauth/github", json=payload())).json()
        second = (
            await client.post(
                "/api/auth/oauth/github", json=payload(email="new@example.invalid")
            )
        ).json()
        assert second["user"]["id"] == first["user"]["id"]


class TestLinkingSafety:
    """The takeover routes this endpoint must not offer."""

    async def test_a_verified_matching_email_does_not_adopt_an_existing_account(
        self, client: AsyncClient, demo_data: dict
    ) -> None:
        """The attack, stated plainly.

        An attacker sets their GitHub address to a known Ledger AI user's
        address, and GitHub reports it verified. If that were enough to resolve
        to the existing account, the attacker would be signed straight into
        someone else's financial data.
        """
        victim = demo_data["user"]

        body = (
            await client.post(
                "/api/auth/oauth/github",
                json=payload(email=victim.email, email_verified=True),
            )
        ).json()

        assert body["user"]["id"] != str(victim.id), (
            "a GitHub identity must never adopt an existing account by email"
        )
        assert body["created"] is True

    async def test_the_victims_account_is_untouched(
        self, client: AsyncClient, demo_data: dict, sync_db: Session
    ) -> None:
        victim = demo_data["user"]
        original_email = victim.email

        await client.post(
            "/api/auth/oauth/github",
            json=payload(email=victim.email, email_verified=True),
        )

        sync_db.expire_all()
        refreshed = sync_db.execute(
            select(User).where(User.id == victim.id)
        ).scalar_one()
        assert refreshed.email == original_email
        assert refreshed.github_id is None

    async def test_a_contested_address_is_not_reused(
        self, client: AsyncClient, demo_data: dict
    ) -> None:
        """The new account gets a placeholder rather than colliding."""
        victim = demo_data["user"]
        body = (
            await client.post(
                "/api/auth/oauth/github",
                json=payload(email=victim.email, email_verified=True),
            )
        ).json()
        assert body["user"]["email"] != victim.email
        assert body["user"]["email"].endswith("@github.ledgerai.invalid")

    async def test_two_github_identities_get_two_accounts(
        self, client: AsyncClient
    ) -> None:
        first = (
            await client.post("/api/auth/oauth/github", json=payload(provider_account_id="1"))
        ).json()
        second = (
            await client.post("/api/auth/oauth/github", json=payload(provider_account_id="2"))
        ).json()
        assert first["user"]["id"] != second["user"]["id"]

    async def test_a_github_account_cannot_sign_in_with_a_password(
        self, client: AsyncClient, sync_db: Session
    ) -> None:
        """No password exists for it, so the credentials path cannot reach it."""
        body = (await client.post("/api/auth/oauth/github", json=payload())).json()

        response = await client.post(
            "/api/auth/login",
            json={"email": body["user"]["email"], "password": ""},
        )
        assert response.status_code in (401, 422)

    async def test_the_response_carries_no_provider_internals(
        self, client: AsyncClient
    ) -> None:
        raw = (await client.post("/api/auth/oauth/github", json=payload())).text.lower()

        for leak in ("access_token", "client_secret", "code=", "state=", "bearer"):
            assert leak not in raw

    @pytest.mark.parametrize(
        "bad",
        [
            {"provider_account_id": ""},
            {"provider_account_id": "x" * 65},
        ],
    )
    async def test_a_malformed_provider_id_is_rejected(
        self, client: AsyncClient, bad: dict
    ) -> None:
        response = await client.post("/api/auth/oauth/github", json=payload(**bad))
        assert response.status_code == 422


class TestIsolation:
    async def test_a_new_github_account_starts_empty(
        self, client: AsyncClient, demo_data: dict
    ) -> None:
        """A fresh identity must not inherit anyone's transactions."""
        from ledgerai.security.jwt import create_access_token

        body = (await client.post("/api/auth/oauth/github", json=payload())).json()
        headers = {
            "Authorization": (
                f"Bearer {create_access_token(body['user']['id'], body['user']['email'])}"
            )
        }

        response = await client.get("/api/transactions", headers=headers)
        assert response.status_code == 200
        assert response.json()["total"] == 0
