"""Nothing sensitive reaches the logs.

Three layers are asserted here: the redaction filter itself, the call-site
convention of logging identifiers rather than content, and the production
decision to stop recording request query strings — because this API's query
strings carry merchant names.
"""

from __future__ import annotations

import inspect
import logging

import pytest

from ledgerai.security.logging import RedactingFilter, install_redaction, redact


class TestRedaction:
    @pytest.mark.parametrize(
        ("raw", "must_not_contain"),
        [
            ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig", "payload"),
            ("token=eyJhbGciOiJIUzI1NiJ9.abcdefghijklmnopqrst", "abcdefghij"),
            ("key sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ", "ABCDEFGHIJKLMNOP"),
            ("postgresql+psycopg://ledgerai:hunter2@db:5432/x", "hunter2"),
            ("GET /api/transactions?search=Blue+Bottle+Coffee", "Blue+Bottle"),
            ("GET /api/transactions?merchant=Whole+Foods", "Whole+Foods"),
            ("ocr <redact>SANDBOX GROCERS TOTAL 30.36</redact>", "30.36"),
            # httpx logs the URL of every Clerk Backend API call, and that URL
            # carries a user id. No application code formats that line, so the
            # log-ids-not-content convention cannot reach it.
            (
                'HTTP Request: GET https://api.clerk.com/v1/users/user_2abcDEFghi123JKLmno '
                '"HTTP/1.1 200 OK"',
                "user_2abcDEFghi123JKLmno",
            ),
            ("session sess_2xyzABCdefGHI9876543 revoked", "sess_2xyzABCdefGHI9876543"),
        ],
    )
    def test_sensitive_substrings_are_removed(self, raw: str, must_not_contain: str) -> None:
        assert must_not_contain not in redact(raw)

    def test_redacting_a_clerk_id_keeps_the_rest_of_the_line(self) -> None:
        """A silenced line is a lost diagnostic. Only the identifier goes."""
        raw = (
            'HTTP Request: GET https://api.clerk.com/v1/users/user_2abcDEFghi123JKLmno '
            '"HTTP/1.1 404 Not Found"'
        )
        cleaned = redact(raw)
        assert "api.clerk.com/v1/users/user_[REDACTED]" in cleaned
        assert "404 Not Found" in cleaned

    def test_an_ordinary_application_error_survives_untouched(self) -> None:
        # Redacting Clerk ids must not become a way of hiding real failures.
        safe = "clerk.jwks_unavailable attempt=2 status=503"
        assert redact(safe) == safe

    def test_a_short_word_ending_in_underscore_is_not_mangled(self) -> None:
        assert redact("user_id=7 org_name=acme") == "user_id=7 org_name=acme"

    def test_safe_lines_are_untouched(self) -> None:
        """Over-redaction would make the logs useless."""
        safe = "Upload 4a4d351d processed: pages=1 status=needs_review imported=47"
        assert redact(safe) == safe

    def test_the_filter_rewrites_the_record(self) -> None:
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="query %s", args=("?search=Blue+Bottle",), exc_info=None,
        )
        assert RedactingFilter().filter(record) is True
        assert "Blue+Bottle" not in record.getMessage()

    def test_a_broken_record_does_not_break_logging(self) -> None:
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="too few args %s %s", args=("one",), exc_info=None,
        )
        # Returning True keeps a malformed record flowing rather than losing it.
        assert RedactingFilter().filter(record) is True

    def test_install_is_idempotent(self) -> None:
        """Calling twice must not stack duplicate filters on a handler."""
        install_redaction()
        install_redaction()
        install_redaction()

        for target in (logging.getLogger(), logging.getLogger("uvicorn.access")):
            for handler in target.handlers:
                filters = [f for f in handler.filters if isinstance(f, RedactingFilter)]
                assert len(filters) == 1


class TestCallSites:
    """The filter is a safety net; call sites still must not log content."""

    FORBIDDEN = (
        "raw_text",
        "result.text",
        "parsed.merchant",
        "total_cents",
        "storage_key",
        "raw_description",
        "auth_secret",
        "access_token",
        "password",
    )

    def test_no_module_logs_sensitive_fields(self) -> None:
        from ledgerai.jobs import process_upload, retention
        from ledgerai.routers import alerts, auth, receipts, settings, transactions, uploads
        from ledgerai.services import lifecycle
        from ledgerai.services import receipts as receipts_service
        from ledgerai.services.ocr import engine, parse, preprocess

        modules = (
            process_upload, retention, alerts, auth, receipts, settings,
            transactions, uploads, lifecycle, receipts_service,
            engine, parse, preprocess,
        )
        for module in modules:
            for line in inspect.getsource(module).splitlines():
                if "logger." not in line:
                    continue
                for token in self.FORBIDDEN:
                    assert token not in line, f"{module.__name__} logs {token}: {line.strip()}"

    def test_the_receipt_log_context_carries_no_content(self) -> None:
        import uuid as _uuid
        from datetime import date as _date

        from ledgerai.models import Receipt, ReceiptStatus
        from ledgerai.services.receipts import receipt_log_context

        context = receipt_log_context(
            Receipt(
                id=_uuid.uuid4(), user_id=_uuid.uuid4(), upload_id=_uuid.uuid4(),
                status=ReceiptStatus.NEEDS_REVIEW, page_count=1,
                raw_text="SANDBOX GROCERS TOTAL 30.36", merchant="Sandbox Grocers",
                posted_date=_date(2026, 8, 14), total_cents=3036, currency="USD",
            )
        )
        rendered = str(context)
        for secret in ("Sandbox Grocers", "30.36", "3036", "2026-08-14"):
            assert secret not in rendered


class TestProductionLogging:
    def test_access_logging_is_configurable(self) -> None:
        from ledgerai.config import settings

        assert hasattr(settings, "enable_access_log")

    def test_the_production_command_disables_access_logs(self) -> None:
        """A URL like /api/transactions?search=... is a merchant name in a log
        line purely by existing, so the server must not record it."""
        import pathlib

        dockerfile = pathlib.Path(__file__).resolve().parents[1] / "Dockerfile"
        assert "--no-access-log" in dockerfile.read_text()
