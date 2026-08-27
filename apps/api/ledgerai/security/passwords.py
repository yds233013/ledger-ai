"""Password hashing (bcrypt directly — no passlib indirection)."""

from __future__ import annotations

import bcrypt

# bcrypt silently truncates at 72 bytes; reject rather than truncate so two
# different long passwords can never authenticate each other.
MAX_PASSWORD_BYTES = 72


def hash_password(plain: str) -> str:
    raw = plain.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        raise ValueError("Password must be at most 72 bytes")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    raw = plain.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(raw, hashed.encode("utf-8"))
    except ValueError:
        return False
