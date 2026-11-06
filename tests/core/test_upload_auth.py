"""
Security tests for the file upload path.

POST /upload accepts a file only with a single-use token minted by the
authenticated create_upload_url tool. The stored file belongs to that user,
is never served back over either /attachments route, and is deleted once
create_drive_file has copied it to Drive.
"""

import asyncio
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient as FastAPITestClient
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import core.attachment_storage as storage_module
from auth.oauth_config import get_oauth_config
from auth.oauth_callback_server import MinimalOAuthServer
from core.attachment_storage import AttachmentStorage, UploadTokenStore
from core.server import serve_attachment, upload_file
from gdrive.drive_tools import create_drive_file, create_upload_url


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """Fresh storage and token store, writing to a temp directory."""
    monkeypatch.setattr(storage_module, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(storage_module, "UPLOADS_DIR", tmp_path / "uploads")
    storage = AttachmentStorage()
    tokens = UploadTokenStore()
    monkeypatch.setattr(storage_module, "_attachment_storage", storage)
    monkeypatch.setattr(storage_module, "_upload_token_store", tokens)
    return storage, tokens


@pytest.fixture
def transport(monkeypatch):
    """Set the server transport mode for one test."""
    return lambda mode: monkeypatch.setattr(get_oauth_config(), "_transport_mode", mode)


@pytest.fixture
def client(stores):
    app = Starlette(
        routes=[
            Route("/upload", upload_file, methods=["POST"]),
            Route("/attachments/{file_id}", serve_attachment, methods=["GET"]),
        ]
    )
    return TestClient(app)


def _post(client, token=None, body=b"%PDF-1.4 test"):
    url = "/upload" if token is None else f"/upload?token={token}"
    return client.post(url, files={"file": ("doc.pdf", body, "application/pdf")})


def _upload(storage, owner="alice@example.com", body=b"%PDF private"):
    return storage.save_bytes(body, filename="p.pdf", mime_type="application/pdf", owner_email=owner)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


class TestUploadTokenStore:
    def test_expired_token_is_rejected(self):
        store = UploadTokenStore(ttl_seconds=-1)
        token, _ = store.issue("alice@example.com")
        assert store.consume(token) is None

    def test_default_token_lives_ten_minutes(self):
        _, expires_at = UploadTokenStore().issue("alice@example.com")
        remaining = (expires_at - datetime.now()).total_seconds()
        assert 590 <= remaining <= 600

    @pytest.mark.parametrize("token", [None, "", "not-a-real-token"])
    def test_missing_or_unknown_token_is_rejected(self, token):
        assert UploadTokenStore().consume(token) is None


# ---------------------------------------------------------------------------
# POST /upload
# ---------------------------------------------------------------------------


class TestUploadRoute:
    @pytest.mark.parametrize("token", [None, "forged"])
    def test_request_without_valid_token_is_rejected_and_nothing_stored(self, client, stores, token):
        storage, _ = stores
        response = _post(client, token=token)
        assert response.status_code == 401
        assert "create_upload_url" in response.json()["error"]
        assert storage._metadata == {}

    def test_valid_token_stores_file_owned_by_minter_once(self, client, stores):
        storage, tokens = stores
        token, _ = tokens.issue("Alice@Example.com")
        response = _post(client, token=token, body=b"%PDF-1.4 hello")
        assert response.status_code == 200
        upload_id = response.json()["upload_id"]
        path, meta = storage.get_owned_upload(upload_id, "alice@example.com")
        assert meta["owner_email"] == "alice@example.com"
        assert path.read_bytes() == b"%PDF-1.4 hello"
        assert _post(client, token=token).status_code == 401

    def test_token_is_checked_before_the_body_is_parsed(self, client, stores):
        storage, _ = stores
        response = client.post(
            "/upload",
            content=b"not a multipart body",
            headers={"content-type": "multipart/form-data; boundary=x"},
        )
        assert response.status_code == 401
        assert storage._metadata == {}

    @pytest.mark.parametrize(
        "headers,status",
        [
            ({"content-type": "application/pdf"}, 400),
            ({"content-length": str(200 * 1024 * 1024)}, 413),
        ],
    )
    def test_header_rejections_leave_the_token_usable(self, client, stores, headers, status):
        storage, tokens = stores
        token, _ = tokens.issue("alice@example.com")
        base = {"content-type": "multipart/form-data; boundary=x", "content-length": "10"}
        response = client.post(
            f"/upload?token={token}", content=b"0123456789", headers={**base, **headers}
        )
        assert response.status_code == status
        assert storage._metadata == {}
        assert _post(client, token=token).status_code == 200

    def test_chunked_body_without_length_is_refused(self, client, stores):
        _, tokens = stores
        token, _ = tokens.issue("alice@example.com")
        response = client.post(
            f"/upload?token={token}",
            content=iter([b"--x\r\n"]),
            headers={"content-type": "multipart/form-data; boundary=x"},
        )
        assert response.status_code == 411
        assert _post(client, token=token).status_code == 200

    def test_missing_file_field_says_the_url_is_used(self, client, stores):
        storage, tokens = stores
        token, _ = tokens.issue("alice@example.com")
        response = client.post(f"/upload?token={token}", files={"other": ("a", b"x")})
        assert response.status_code == 400
        assert "create_upload_url" in response.json()["error"]
        assert storage._metadata == {}
        assert _post(client, token=token).status_code == 401

    def test_upload_sweeps_expired_files(self, client, stores):
        storage, tokens = stores
        stale = storage.save_bytes(b"old", filename="old.txt")
        storage._metadata[stale.file_id]["expires_at"] = datetime.now() - timedelta(seconds=1)
        token, _ = tokens.issue("alice@example.com")
        assert _post(client, token=token).status_code == 200
        assert stale.file_id not in storage._metadata
        assert not Path(stale.path).exists()


# ---------------------------------------------------------------------------
# GET /attachments/{id}, both servers
# ---------------------------------------------------------------------------


class TestAttachmentRoutesRefuseUploads:
    def test_main_server_does_not_serve_uploads(self, client, stores):
        storage, _ = stores
        assert client.get(f"/attachments/{_upload(storage).file_id}").status_code == 404

    def test_oauth_callback_server_does_not_serve_uploads(self, stores):
        storage, _ = stores
        callback_client = FastAPITestClient(MinimalOAuthServer().app)
        assert callback_client.get(f"/attachments/{_upload(storage).file_id}").status_code == 404

    def test_server_generated_attachment_is_still_served(self, client, stores):
        storage, _ = stores
        saved = storage.save_bytes(b"gmail bytes", filename="a.txt", mime_type="text/plain")
        response = client.get(f"/attachments/{saved.file_id}")
        assert response.status_code == 200
        assert response.content == b"gmail bytes"

    def test_expired_or_missing_attachment_is_not_served(self, client, stores):
        storage, _ = stores
        expired = storage.save_bytes(b"old", filename="a.txt")
        storage._metadata[expired.file_id]["expires_at"] = datetime.now() - timedelta(seconds=1)
        missing = storage.save_bytes(b"gone", filename="b.txt")
        Path(missing.path).unlink()
        assert client.get(f"/attachments/{expired.file_id}").status_code == 404
        assert client.get(f"/attachments/{missing.file_id}").status_code == 404


class TestPrivateUploadDirectory:
    """Uploads are kept apart so file:// paths cannot bypass the owner check."""

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("ALLOWED_FILE_DIRS", str(tmp_path))
        monkeypatch.delenv("WORKSPACE_MCP_CREDENTIALS_DIR", raising=False)
        monkeypatch.delenv("GOOGLE_MCP_CREDENTIALS_DIR", raising=False)
        return tmp_path

    def test_uploads_are_stored_apart_from_server_attachments(self, stores, tmp_path):
        storage, _ = stores
        upload = _upload(storage)
        attachment = storage.save_bytes(b"gmail bytes", filename="a.txt")
        assert Path(upload.path).parent == (tmp_path / "uploads").resolve()
        assert Path(attachment.path).parent == tmp_path.resolve()

    def test_mixed_case_owner_is_normalized_on_save(self, stores):
        storage, _ = stores
        saved = _upload(storage, owner="Alice@Example.COM")
        assert storage.get_owned_upload(saved.file_id, "alice@example.com") is not None

    def test_stdio_blocks_uploads_but_allows_downloaded_attachments(self, stores, home, transport):
        from core.utils import validate_file_path

        transport("stdio")
        storage, _ = stores
        upload = _upload(storage)
        attachment = storage.save_bytes(b"gmail bytes", filename="a.txt")
        with pytest.raises(ValueError, match="private attachment storage"):
            validate_file_path(upload.path)
        assert validate_file_path(attachment.path) == Path(attachment.path)

    def test_shared_http_server_blocks_all_stored_files(self, stores, home, transport):
        from core.utils import validate_file_path

        transport("streamable-http")
        storage, _ = stores
        for saved in (_upload(storage), storage.save_bytes(b"gmail bytes", filename="a.txt")):
            with pytest.raises(ValueError, match="private attachment storage"):
                validate_file_path(saved.path)

    def test_case_variant_path_is_still_blocked(self, stores, home, transport):
        from core.utils import validate_file_path

        transport("stdio")
        storage, _ = stores
        upload = _upload(storage)
        variant = Path(upload.path).parent.parent / "UPLOADS" / Path(upload.path).name
        if not variant.exists():
            pytest.skip("case-sensitive filesystem")
        with pytest.raises(ValueError, match="private attachment storage"):
            validate_file_path(str(variant))

    def test_symlinked_secret_dir_is_blocked_through_its_target(self, home):
        from core.utils import validate_file_path

        real = home / "keys"
        real.mkdir()
        (real / "id_rsa").write_text("secret")
        (home / ".ssh").symlink_to(real)
        with pytest.raises(ValueError, match="commonly contains secrets"):
            validate_file_path(str(real / "id_rsa"))

    def test_missing_file_in_blocked_dir_does_not_reveal_existence(self, stores, home, transport):
        from core.utils import validate_file_path

        transport("stdio")
        with pytest.raises(ValueError, match="private attachment storage"):
            validate_file_path(str(home / "uploads" / "nobody.pdf"))


# ---------------------------------------------------------------------------
# create_upload_url tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_upload_url_returns_single_use_url_bound_to_caller(stores, monkeypatch, transport):
    _, tokens = stores
    transport("streamable-http")
    monkeypatch.setattr("gdrive.drive_tools.is_stateless_mode", lambda: False)
    monkeypatch.setenv("WORKSPACE_EXTERNAL_URL", "https://mcp.example.com/")
    result = await _unwrap(create_upload_url)(Mock(), user_google_email="Bob@Example.com")
    assert result.startswith("https://mcp.example.com/upload?token=")
    token = result.split("token=")[1].split()[0]
    assert tokens.consume(token) == "bob@example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,stateless,match", [
    ("stdio", False, "streamable-http"),
    ("streamable-http", True, "stateless"),
])
async def test_create_upload_url_refuses_where_uploads_cannot_work(
    stores, monkeypatch, transport, mode, stateless, match
):
    _, tokens = stores
    transport(mode)
    monkeypatch.setattr("gdrive.drive_tools.is_stateless_mode", lambda: stateless)
    with pytest.raises(Exception, match=match):
        await _unwrap(create_upload_url)(Mock(), user_google_email="bob@example.com")
    assert tokens._tokens == {}


# ---------------------------------------------------------------------------
# create_drive_file with upload_id
# ---------------------------------------------------------------------------


def _drive_service(fail=False):
    service = Mock()
    execute = service.files().create().execute
    if fail:
        execute.side_effect = RuntimeError("drive down")
    else:
        execute.return_value = {
            "id": "file_123",
            "name": "doc.pdf",
            "webViewLink": "https://drive.google.com/file/d/file_123/view",
        }
    return service


async def _create(upload_id, caller, service=None):
    with patch("gdrive.drive_tools.resolve_folder_id", new_callable=AsyncMock, return_value="root"):
        return await _unwrap(create_drive_file)(
            service or _drive_service(),
            user_google_email=caller,
            file_name="doc.pdf",
            upload_id=upload_id,
        )


class TestCreateDriveFileUploadOwnership:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("owner", ["alice@example.com", None])
    async def test_upload_not_owned_by_caller_reads_as_not_found(self, stores, owner):
        storage, _ = stores
        saved = _upload(storage, owner=owner)
        with pytest.raises(Exception, match="Upload not found or expired"):
            await _create(saved.file_id, "mallory@example.com")
        assert Path(saved.path).exists()

    @pytest.mark.asyncio
    async def test_owner_can_use_upload_once_and_file_is_deleted(self, stores):
        storage, _ = stores
        saved = _upload(storage)
        assert "Successfully created file" in await _create(saved.file_id, "Alice@Example.com")
        assert not Path(saved.path).exists()
        with pytest.raises(Exception, match="Upload not found or expired"):
            await _create(saved.file_id, "alice@example.com")

    @pytest.mark.asyncio
    async def test_failed_drive_create_keeps_upload_for_retry(self, stores):
        storage, _ = stores
        saved = _upload(storage)
        with pytest.raises(RuntimeError):
            await _create(saved.file_id, "alice@example.com", service=_drive_service(fail=True))
        assert Path(saved.path).exists()
        assert "Successfully created file" in await _create(saved.file_id, "alice@example.com")
        assert not Path(saved.path).exists()

    @pytest.mark.asyncio
    async def test_upload_is_claimed_while_drive_create_runs(self, stores):
        storage, _ = stores
        saved = _upload(storage)
        seen = []
        service = _drive_service()
        execute = service.files().create().execute
        result = execute.return_value

        def during_drive_call():
            seen.append(storage.get_owned_upload(saved.file_id, "alice@example.com"))
            return result

        execute.side_effect = during_drive_call
        await _create(saved.file_id, "alice@example.com", service=service)
        assert seen == [None]

    @pytest.mark.asyncio
    async def test_cancelled_call_keeps_upload_claimed_until_drive_finishes(self, stores):
        storage, _ = stores
        saved = _upload(storage)
        started, finish = threading.Event(), threading.Event()
        service = _drive_service()
        execute = service.files().create().execute
        result = execute.return_value

        def slow_drive_call():
            started.set()
            finish.wait(5)
            return result

        execute.side_effect = slow_drive_call
        task = asyncio.create_task(_create(saved.file_id, "alice@example.com", service=service))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert storage.get_owned_upload(saved.file_id, "alice@example.com") is None
        finish.set()
        for _ in range(100):
            if not Path(saved.path).exists():
                break
            await asyncio.sleep(0.02)
        assert not Path(saved.path).exists()
        assert saved.file_id not in storage._metadata

    @pytest.mark.asyncio
    @pytest.mark.parametrize("problem", ["expired", "missing"])
    async def test_expired_or_missing_upload_reads_as_not_found(self, stores, problem):
        storage, _ = stores
        saved = _upload(storage)
        if problem == "expired":
            storage._metadata[saved.file_id]["expires_at"] = datetime.now() - timedelta(seconds=1)
        else:
            Path(saved.path).unlink()
        with pytest.raises(Exception, match="Upload not found or expired"):
            await _create(saved.file_id, "alice@example.com")
