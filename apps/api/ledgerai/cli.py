"""Operational entry points that ship *inside* the API image.

The scheduled sweeps were previously documented as `python scripts/retention_sweep.py`,
which cannot work in a deployed container. The image is built from the
`apps/api` context and copies only `ledgerai/`, `alembic/`, `alembic.ini` and
the lock files — the repository-root `scripts/` directory is not in it. The
command failed with "No such file or directory", and it failed *quietly* as far
as anyone using the app was concerned: nothing surfaces the fact that expired
demo accounts have stopped being reaped until the database has grown for weeks.

These functions are installed as console scripts (see `[project.scripts]`), so
the deployed command is `ledgerai-demo-cleanup` — on PATH inside the image,
assuming nothing about the repository layout. `python -m ledgerai.cli <command>`
is the equivalent for anyone who would rather not depend on the virtualenv's
bin directory being on PATH.

Exit status is what a scheduler reads. A sweep that raises exits non-zero so
the platform records a failed run, rather than printing a traceback and
reporting success.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable, Mapping, Sequence

from .security.logging import install_redaction

logger = logging.getLogger("ledgerai.cli")

# Report dicts are small and flat; a scheduler's log pane is the only reader.
_JSON_INDENT = 2


def _run(name: str, job: Callable[[], Mapping[str, object]]) -> int:
    """Run one sweep, print its report as JSON, and translate failure to status.

    Logging is configured here rather than at import time because importing
    this module must not reconfigure logging for the API process, which shares
    the package.
    """
    logging.basicConfig(level="INFO", format="%(levelname)-5s %(message)s")
    # The sweeps touch users, storage keys and connection strings. The
    # redaction filter is not optional in a context whose entire output is
    # captured by a platform log viewer.
    install_redaction()

    try:
        report = job()
    except Exception:
        # No `raise`: a traceback through a cron runner is noise, and the
        # message is already logged with its stack by the handler below.
        logger.exception("%s failed", name)
        return 1

    print(json.dumps(report, indent=_JSON_INDENT, default=str))
    return 0


def demo_cleanup_main() -> int:
    """Delete ephemeral demo accounts past their 24-hour deadline."""
    from .jobs.demo_cleanup import run_demo_cleanup

    return _run("demo-cleanup", run_demo_cleanup)


def retention_sweep_main() -> int:
    """Fail abandoned jobs, purge failed-upload files, drop stale receipts."""
    from .jobs.retention import run_retention_sweep

    return _run("retention-sweep", run_retention_sweep)


def backfill_categories_main() -> int:
    """Re-categorize rows left uncategorized while the taxonomy was missing.

    One-off repair, not a scheduled sweep: once the taxonomy exists, imports
    categorize correctly on their own. Safe to run more than once — the second
    run finds nothing eligible.
    """
    from .jobs.backfill import run_category_backfill

    return _run("backfill-categories", run_category_backfill)


def invite_main() -> int:
    """Administrative invitation management for the private beta.

        ledgerai-invite create <email> [--days N] [--note "..."]
        ledgerai-invite revoke <email>
        ledgerai-invite list

    The address is never stored — only a keyed HMAC of it and a lossy hint —
    so `list` shows hints rather than addresses. That is the point: the table
    must not be a directory of who was invited.
    """
    import argparse

    from .db import sync_session
    from .services.identity import (
        DEFAULT_INVITE_TTL_DAYS,
        create_invitation,
        revoke_invitation,
    )

    logging.basicConfig(level="INFO", format="%(levelname)-5s %(message)s")
    install_redaction()

    parser = argparse.ArgumentParser(prog="ledgerai-invite")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("email")
    create.add_argument("--days", type=int, default=DEFAULT_INVITE_TTL_DAYS)
    create.add_argument("--note", default="")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("email")
    sub.add_parser("list")

    args = parser.parse_args(sys.argv[1:])

    from sqlalchemy import select

    from .models import Invitation

    with sync_session() as session:
        if args.command == "create":
            invitation = create_invitation(
                session, args.email, ttl_days=args.days, note=args.note
            )
            session.commit()
            print(
                json.dumps(
                    {
                        "created": True,
                        "hint": invitation.email_hint,
                        "expires_at": str(invitation.expires_at),
                    },
                    indent=_JSON_INDENT,
                )
            )
            print(
                "\nNow send the Clerk invitation to the same address. "
                "The user enters no code — provisioning matches on the email "
                "Clerk verifies.",
                file=sys.stderr,
            )
            return 0

        if args.command == "revoke":
            ok = revoke_invitation(session, args.email)
            session.commit()
            print(json.dumps({"revoked": ok}, indent=_JSON_INDENT))
            return 0 if ok else 1

        rows = session.execute(select(Invitation).order_by(Invitation.created_at)).scalars()
        print(
            json.dumps(
                [
                    {
                        "hint": r.email_hint,
                        "expires_at": str(r.expires_at),
                        "redeemed": r.redeemed_at is not None,
                        "revoked": r.revoked_at is not None,
                        "note": r.note,
                    }
                    for r in rows
                ],
                indent=_JSON_INDENT,
            )
        )
        return 0


# The single source of truth for what an operator may schedule. The deployment
# configuration tests read this, so a command that is documented but not
# defined here fails the test suite rather than a cron run at 04:00.
COMMANDS: dict[str, Callable[[], int]] = {
    "demo-cleanup": demo_cleanup_main,
    "retention-sweep": retention_sweep_main,
    "backfill-categories": backfill_categories_main,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in COMMANDS:
        available = ", ".join(sorted(COMMANDS))
        print(f"usage: python -m ledgerai.cli <{available}>", file=sys.stderr)
        return 2
    return COMMANDS[args[0]]()


if __name__ == "__main__":
    sys.exit(main())
