"""Filename sanitization, password hashing and token verification."""

from __future__ import annotations

import uuid

import pytest

from ledgerai.security.filenames import build_storage_key, sanitize_filename
from ledgerai.security.jwt import TokenError, create_access_token, decode_access_token
from ledgerai.security.passwords import hash_password, verify_password
from ledgerai.security.validators import ValidationError, detect_kind, validate_csv_structure
from ledgerai.services.storage import StorageError, _assert_safe_key


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\cfg.ini", "cfg.ini"),
        ("/abs/path/statement.csv", "statement.csv"),
        ("....//x.csv", "x.csv"),
        ("", "upload"),
        ("..", "upload"),
        (".hidden", "hidden"),
        ("shell$(whoami);rm -rf.csv", "shell-whoami-rm--rf.csv"),
    ],
)
def test_sanitize_filename_removes_paths(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitized_name_never_contains_separators() -> None:
    for raw in ["a/b/c.csv", "a\\b\\c.csv", "../../../x", "%2e%2e/x.csv"]:
        result = sanitize_filename(raw)
        assert "/" not in result
        assert "\\" not in result
        assert ".." not in result


def test_storage_key_ignores_user_supplied_structure() -> None:
    user_id = uuid.uuid4()
    key = build_storage_key(user_id, sanitize_filename("../../evil.csv"))
    assert key.startswith(f"users/{user_id}/uploads/")
    assert ".." not in key
    _assert_safe_key(key)  # must not raise


@pytest.mark.parametrize(
    "key", ["../../etc/passwd", "users/x/uploads/y/z.csv", "/absolute", "users/../x"]
)
def test_storage_rejects_unsafe_keys(key: str) -> None:
    with pytest.raises(StorageError):
        _assert_safe_key(key)


def test_password_round_trip() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong", hashed)


def test_password_length_is_rejected_not_truncated() -> None:
    """bcrypt truncates at 72 bytes; two different long passwords must not
    become interchangeable."""
    with pytest.raises(ValueError):
        hash_password("x" * 100)


def test_token_round_trip() -> None:
    user_id = uuid.uuid4()
    claims = decode_access_token(create_access_token(user_id, "a@b.test"))
    assert claims["sub"] == str(user_id)
    assert claims["aud"] == "ledgerai-api"


@pytest.mark.parametrize("token", ["", "garbage", "a.b.c"])
def test_invalid_tokens_are_rejected(token: str) -> None:
    with pytest.raises(TokenError):
        decode_access_token(token)


def test_expired_token_is_rejected() -> None:
    expired = create_access_token(uuid.uuid4(), "a@b.test", ttl_minutes=-1)
    with pytest.raises(TokenError):
        decode_access_token(expired)


def test_csv_structure_validation_accepts_common_layouts() -> None:
    signed = b"Date,Description,Amount\n2026-01-01,COFFEE,-4.50\n"
    assert validate_csv_structure(signed)["amount"] == "Amount"

    debit_credit = b"Posting Date;Narrative;Debit;Credit\n01/01/2026;COFFEE;4.50;\n"
    assert "date" in validate_csv_structure(debit_credit)


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"", "empty"),
        (b"Date,Description\n2026-01-01,COFFEE\n", "missing amount"),
        (b"Date,Description,Amount\n", "header only"),
    ],
)
def test_csv_structure_validation_rejects_bad_files(data: bytes, reason: str) -> None:
    with pytest.raises(ValidationError):
        validate_csv_structure(data)


def test_content_sniffing_beats_extension() -> None:
    """A PNG renamed to .csv must be treated as an image, not parsed as text."""
    png = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64
    kind, mime = detect_kind("statement.csv", png)
    assert mime == "image/png"


def test_executable_disguised_as_csv_is_rejected() -> None:
    with pytest.raises(ValidationError):
        detect_kind("payload.exe", b"MZ\x90\x00" + b"\x00" * 64)
