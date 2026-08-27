"""Storage backend selection, and the fallback that must not happen.

`get_storage()` falls back to local disk when object storage is unreachable.
That is right in development — someone who cloned the repo without Docker still
gets a working upload path — and catastrophic in production, where the API and
the worker are separate containers with separate filesystems.

The failure it produces is the quiet kind. The API writes a receipt to its own
disk and returns 200. The worker looks for that file on a different disk and
every OCR job fails. Nothing reports a storage problem, because as far as the
application is concerned the write succeeded, and the container's disk is
discarded on the next deploy either way.

So in production the fallback is refused and the error is allowed to surface.
"""

from __future__ import annotations

import pytest

from ledgerai.config import settings
from ledgerai.services import storage as storage_module
from ledgerai.services.storage import (
    LocalStorage,
    StorageError,
    get_storage,
    reset_storage,
)


@pytest.fixture(autouse=True)
def _clean_storage_singleton():
    """The backend is chosen once per process, so tests must discard it."""
    reset_storage(None)
    yield
    reset_storage(None)


@pytest.fixture
def unreachable_object_storage(monkeypatch):
    """Make constructing the S3 backend fail the way a bad endpoint does."""

    def _explode(self) -> None:
        raise StorageError("endpoint unreachable")

    monkeypatch.setattr(storage_module.S3Storage, "__init__", _explode)


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production", raising=False)


class TestBackendSelection:
    def test_local_is_chosen_explicitly(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(settings, "storage_backend", "local", raising=False)
        monkeypatch.setattr(settings, "local_storage_dir", str(tmp_path), raising=False)
        assert isinstance(get_storage(), LocalStorage)

    def test_any_other_value_means_the_s3_adapter(self, monkeypatch) -> None:
        """R2 is configured as `minio` — the name is the local service, not the
        protocol. A separate backend name for every S3-compatible vendor would
        be a lie about there being separate code paths."""
        constructed: list[str] = []

        def _record(self) -> None:
            constructed.append("s3")

        monkeypatch.setattr(storage_module.S3Storage, "__init__", _record)
        monkeypatch.setattr(settings, "storage_backend", "minio", raising=False)
        get_storage()
        assert constructed == ["s3"]


class TestProductionRefusesTheLocalFallback:
    def test_it_raises_instead_of_writing_to_a_disk_the_worker_cannot_read(
        self, monkeypatch, production, unreachable_object_storage
    ) -> None:
        monkeypatch.setattr(settings, "storage_backend", "minio", raising=False)
        with pytest.raises(StorageError):
            get_storage()

    def test_the_failure_is_not_cached_as_a_working_backend(
        self, monkeypatch, production, unreachable_object_storage
    ) -> None:
        """A half-initialised singleton would turn one outage into a permanent
        local-disk deployment until the next restart."""
        monkeypatch.setattr(settings, "storage_backend", "minio", raising=False)
        with pytest.raises(StorageError):
            get_storage()
        assert storage_module._storage is None

    def test_the_reason_is_logged_at_critical(
        self, monkeypatch, production, unreachable_object_storage, caplog
    ) -> None:
        monkeypatch.setattr(settings, "storage_backend", "minio", raising=False)
        with caplog.at_level("CRITICAL"), pytest.raises(StorageError):
            get_storage()
        assert "container-local disk" in caplog.text


class TestDevelopmentKeepsTheFallback:
    def test_an_unreachable_endpoint_degrades_to_local(
        self, monkeypatch, tmp_path, unreachable_object_storage
    ) -> None:
        monkeypatch.setattr(settings, "environment", "development", raising=False)
        monkeypatch.setattr(settings, "storage_backend", "minio", raising=False)
        monkeypatch.setattr(settings, "local_storage_dir", str(tmp_path), raising=False)
        assert isinstance(get_storage(), LocalStorage)


class TestBucketCreation:
    """Auto-creation is a local convenience and a production mistake.

    A Cloudflare R2 API token scoped to one bucket — the recommended shape —
    has no CreateBucket permission, so attempting it turns a clear "the bucket
    is missing" into a confusing "access denied".
    """

    def _client_whose_bucket_is_absent(self, monkeypatch):
        from botocore.exceptions import ClientError

        calls: list[str] = []

        class FakeClient:
            def head_bucket(self, Bucket: str) -> None:  # noqa: N803 - boto3 kwarg
                calls.append("head")
                raise ClientError(
                    {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadBucket"
                )

            def create_bucket(self, Bucket: str) -> None:  # noqa: N803 - boto3 kwarg
                calls.append("create")

        monkeypatch.setattr(
            storage_module.boto3, "client", lambda *a, **k: FakeClient()
        )
        return calls

    def test_production_reports_the_missing_bucket_and_does_not_create_it(
        self, monkeypatch, production
    ) -> None:
        calls = self._client_whose_bucket_is_absent(monkeypatch)
        with pytest.raises(StorageError, match="not reachable"):
            storage_module.S3Storage()
        assert calls == ["head"]

    def test_development_creates_it_so_make_up_needs_no_manual_step(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings, "environment", "development", raising=False)
        calls = self._client_whose_bucket_is_absent(monkeypatch)
        storage_module.S3Storage()
        assert calls == ["head", "create"]


class TestR2ShapedConfiguration:
    """The R2 settings documented in .env.example must actually select S3."""

    def test_an_r2_endpoint_and_auto_region_are_accepted(self, monkeypatch) -> None:
        captured: dict[str, object] = {}

        class FakeClient:
            def head_bucket(self, Bucket: str) -> None:  # noqa: N803 - boto3 kwarg
                return None

        def _capture(service: str, **kwargs):
            captured.update(kwargs)
            return FakeClient()

        monkeypatch.setattr(storage_module.boto3, "client", _capture)
        monkeypatch.setattr(settings, "storage_backend", "minio", raising=False)
        monkeypatch.setattr(
            settings,
            "s3_endpoint_url",
            "https://accountid.r2.cloudflarestorage.com",
            raising=False,
        )
        monkeypatch.setattr(settings, "s3_region", "auto", raising=False)

        assert isinstance(get_storage(), storage_module.S3Storage)
        assert captured["endpoint_url"] == "https://accountid.r2.cloudflarestorage.com"
        assert captured["region_name"] == "auto"
        # R2 supports path-style addressing; virtual-host style would require a
        # per-bucket DNS name that does not exist on the R2 endpoint.
        assert captured["config"].s3["addressing_style"] == "path"
        assert captured["config"].signature_version == "s3v4"
