"""Test fixtures.

Unit tests run with no database. The integration tests use the same Postgres
that Compose provides, in a separate `ledgerai_test` database that is created
and migrated once per session, so tests never touch development data.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from ledgerai.config import settings
from ledgerai.db import get_db
from ledgerai.main import app
from ledgerai.models import Account, Base, Category, Transaction, User
from ledgerai.security.jwt import create_access_token
from ledgerai.security.passwords import hash_password
from ledgerai.services.ingest import load_category_definitions
from ledgerai.services.normalize import (
    compute_dedupe_hash,
    merchant_key,
    normalize_description,
)

TEST_DB = "ledgerai_test"


def _url(database: str, *, is_async: bool) -> str:
    base = settings.sync_database_url.rsplit("/", 1)[0]
    return f"{base}/{database}"


@pytest.fixture(scope="session")
def _test_database() -> Iterator[None]:
    admin = create_engine(_url("postgres", is_async=False), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    admin.dispose()

    engine = create_engine(_url(TEST_DB, is_async=False))
    Base.metadata.create_all(engine)
    engine.dispose()
    yield

    admin = create_engine(_url("postgres", is_async=False), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def sync_db(_test_database: None) -> Iterator[Session]:
    engine = create_engine(_url(TEST_DB, is_async=False))
    factory = sessionmaker(engine, expire_on_commit=False)
    session = factory()
    _truncate(session)
    _seed_categories(session)
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest_asyncio.fixture
async def async_db(_test_database: None) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(_url(TEST_DB, is_async=True))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _truncate(session: Session) -> None:
    session.execute(
        text(
            "TRUNCATE "
            + ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
            + " RESTART IDENTITY CASCADE"
        )
    )


def _seed_categories(session: Session) -> None:
    for definition in load_category_definitions():
        session.add(
            Category(
                id=uuid.uuid4(),
                user_id=None,
                name=definition["name"],
                slug=definition["slug"],
                color=definition["color"],
                icon=definition["icon"],
                sort_order=definition["sort_order"],
                is_system=True,
            )
        )


def make_user(session: Session, email: str = "user@test.local") -> User:
    user = User(
        email=email,
        password_hash=hash_password("test-password"),
        display_name="Test User",
        is_demo=True,
    )
    session.add(user)
    session.flush()
    return user


def make_account(session: Session, user: User, name: str = "SANDBOX — Checking") -> Account:
    account = Account(
        user_id=user.id,
        name=name,
        institution="Sandbox",
        account_type="checking",
        mask="0001",
    )
    session.add(account)
    session.flush()
    return account


def make_transaction(  # noqa: PLR0913
    session: Session,
    user: User,
    account: Account,
    *,
    posted: date,
    cents: int,
    description: str,
    merchant: str,
    category_slug: str | None = None,
    index: int = 0,
    currency: str = "USD",
) -> Transaction:
    category_id = None
    if category_slug:
        category_id = session.execute(
            text("SELECT id FROM categories WHERE slug = :slug AND user_id IS NULL"),
            {"slug": category_slug},
        ).scalar_one()
    normalized = normalize_description(description)
    transaction = Transaction(
        user_id=user.id,
        account_id=account.id,
        posted_date=posted,
        amount_cents=cents,
        raw_description=description,
        normalized_description=normalized,
        merchant=merchant,
        merchant_key=merchant_key(merchant),
        currency=currency,
        category_id=category_id,
        confidence=1 if category_slug else 0,
        needs_review=category_slug is None,
        dedupe_hash=compute_dedupe_hash(
            user.id, account.id, posted, cents, normalized, index
        ),
        source_row_index=index,
    )
    session.add(transaction)
    session.flush()
    return transaction


@pytest.fixture
def demo_data(sync_db: Session) -> dict:
    """A small, fully-known dataset so expected totals are hand-checkable."""
    user = make_user(sync_db)
    other = make_user(sync_db, "other@test.local")
    account = make_account(sync_db, user)
    other_account = make_account(sync_db, other, "SANDBOX — Other")

    july = date(2026, 7, 1)
    june = date(2026, 6, 1)

    # July groceries: 40.00 + 60.00 = 100.00
    make_transaction(sync_db, user, account, posted=july + timedelta(days=3),
                     cents=-4000, description="WHOLE FOODS MKT", merchant="Whole Foods MKT",
                     category_slug="groceries", index=1)
    make_transaction(sync_db, user, account, posted=july + timedelta(days=10),
                     cents=-6000, description="TRADER JOES", merchant="Trader Joes",
                     category_slug="groceries", index=2)
    # June groceries: 80.00
    make_transaction(sync_db, user, account, posted=june + timedelta(days=5),
                     cents=-8000, description="WHOLE FOODS MKT", merchant="Whole Foods MKT",
                     category_slug="groceries", index=3)
    # July dining: 25.00 + 18.00 + 12.00 across three Sweetgreen rows, so bulk
    # correction tests have real siblings to act on.
    make_transaction(sync_db, user, account, posted=july + timedelta(days=6),
                     cents=-2500, description="SWEETGREEN", merchant="Sweetgreen",
                     category_slug="dining", index=4)
    make_transaction(sync_db, user, account, posted=july + timedelta(days=12),
                     cents=-1800, description="SWEETGREEN", merchant="Sweetgreen",
                     category_slug="dining", index=8)
    make_transaction(sync_db, user, account, posted=july + timedelta(days=18),
                     cents=-1200, description="SWEETGREEN", merchant="Sweetgreen",
                     category_slug="dining", index=9)
    # The other user has a Sweetgreen row too — same merchant key, different
    # owner. A bulk correction must never reach it.
    make_transaction(sync_db, other, other_account, posted=july + timedelta(days=6),
                     cents=-3300, description="SWEETGREEN", merchant="Sweetgreen",
                     category_slug="dining", index=10)
    # July transfer: 500.00 — must NOT count as spending
    make_transaction(sync_db, user, account, posted=july + timedelta(days=8),
                     cents=-50000, description="ONLINE TRANSFER TO SAVINGS",
                     merchant="Online Transfer", category_slug="transfers", index=5)
    # July income
    make_transaction(sync_db, user, account, posted=july + timedelta(days=1),
                     cents=300000, description="PAYROLL", merchant="Payroll",
                     category_slug="income", index=6)
    # Another user's row — must never appear in the first user's results
    make_transaction(sync_db, other, other_account, posted=july + timedelta(days=4),
                     cents=-99999, description="OTHER USER SECRET", merchant="Other Secret",
                     category_slug="groceries", index=7)

    # A EUR charge the user also holds. Ledger AI does not convert, so this
    # must be excluded from USD totals and disclosed, never silently summed.
    make_transaction(sync_db, user, account, posted=july + timedelta(days=9),
                     cents=-7000, description="SANDBOX BOOKS EU", merchant="Sandbox Books EU",
                     category_slug="shopping", index=11, currency="EUR")

    sync_db.commit()
    return {"user": user, "other": other, "account": account}


@pytest_asyncio.fixture
async def client(demo_data: dict) -> AsyncIterator[AsyncClient]:
    """HTTP client bound to the test database."""
    engine = create_async_engine(_url(TEST_DB, is_async=True))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.fixture
def auth_headers(demo_data: dict) -> dict[str, str]:
    user = demo_data["user"]
    return {"Authorization": f"Bearer {create_access_token(user.id, user.email)}"}


@pytest.fixture
def other_headers(demo_data: dict) -> dict[str, str]:
    other = demo_data["other"]
    return {"Authorization": f"Bearer {create_access_token(other.id, other.email)}"}
