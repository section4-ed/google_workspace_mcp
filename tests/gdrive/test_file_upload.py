"""
Unit tests for file upload functionality.

Tests:
- AttachmentStorage.save_bytes() method
- Path traversal prevention in _save()
- create_drive_file with upload_id parameter
- create_drive_file with file_content_base64 parameter
- Upload endpoint validation logic
"""

import base64
import os
import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from pathlib import Path
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.attachment_storage import AttachmentStorage
from gdrive.drive_tools import create_drive_file


def _unwrap(tool):
    """Unwrap a FunctionTool + decorator chain to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


# ---------------------------------------------------------------------------
# AttachmentStorage.save_bytes
# ---------------------------------------------------------------------------


class TestSaveBytes:
    def setup_method(self):
        self.storage = AttachmentStorage(expiration_seconds=3600)

    def teardown_method(self):
        # Clean up any files created during tests
        for file_id, meta in list(self.storage._metadata.items()):
            path = Path(meta["file_path"])
            if path.exists():
                path.unlink()

    def test_save_bytes_returns_file_id_and_path(self):
        result = self.storage.save_bytes(b"hello world", filename="test.txt")
        assert result.file_id is not None
        assert result.path is not None
        assert Path(result.path).exists()

    def test_save_bytes_content_matches(self):
        result = self.storage.save_bytes(b"binary data \x00\xff", filename="bin.dat")
        assert Path(result.path).read_bytes() == b"binary data \x00\xff"

    def test_save_bytes_stores_metadata(self):
        result = self.storage.save_bytes(
            b"test", filename="doc.pdf", mime_type="application/pdf"
        )
        meta = self.storage.get_attachment_metadata(result.file_id)
        assert meta is not None
        assert meta["filename"] == "doc.pdf"
        assert meta["mime_type"] == "application/pdf"
        assert meta["size"] == 4

    def test_save_bytes_empty_file(self):
        result = self.storage.save_bytes(b"", filename="empty.txt")
        assert Path(result.path).exists()
        assert Path(result.path).read_bytes() == b""

    def test_save_bytes_no_filename(self):
        result = self.storage.save_bytes(b"data", mime_type="image/png")
        assert result.file_id in Path(result.path).name

    def test_save_bytes_retrieval_via_get_attachment_path(self):
        result = self.storage.save_bytes(b"retrieve me", filename="r.txt")
        path = self.storage.get_attachment_path(result.file_id)
        assert path is not None
        assert path.read_bytes() == b"retrieve me"


# ---------------------------------------------------------------------------
# Path traversal prevention
# ---------------------------------------------------------------------------


class TestPathTraversal:
    def setup_method(self):
        self.storage = AttachmentStorage(expiration_seconds=3600)

    def teardown_method(self):
        for file_id, meta in list(self.storage._metadata.items()):
            path = Path(meta["file_path"])
            if path.exists():
                path.unlink()

    def test_directory_traversal_in_filename_stripped(self):
        """Filenames with ../ should have directory components stripped."""
        result = self.storage.save_bytes(b"safe", filename="../../etc/passwd")
        # The file should be saved in the storage dir, not escaped
        from core.attachment_storage import STORAGE_DIR

        assert Path(result.path).resolve().is_relative_to(STORAGE_DIR.resolve())

    def test_absolute_path_filename_stripped(self):
        """Absolute path filenames should have directory components stripped."""
        result = self.storage.save_bytes(b"safe", filename="/etc/shadow")
        from core.attachment_storage import STORAGE_DIR

        assert Path(result.path).resolve().is_relative_to(STORAGE_DIR.resolve())

    def test_nested_traversal_stripped(self):
        """Deeply nested traversal should be stripped."""
        result = self.storage.save_bytes(
            b"safe", filename="../../../tmp/evil.sh"
        )
        from core.attachment_storage import STORAGE_DIR

        assert Path(result.path).resolve().is_relative_to(STORAGE_DIR.resolve())


# ---------------------------------------------------------------------------
# create_drive_file with upload_id
# ---------------------------------------------------------------------------


def _make_mock_drive_service(created_file=None):
    """Create a mock Drive service for file creation."""
    if created_file is None:
        created_file = {
            "id": "file_123",
            "name": "test.txt",
            "webViewLink": "https://drive.google.com/file/d/file_123/view",
        }
    mock_service = Mock()
    mock_service.files().create().execute.return_value = created_file
    mock_service.files().list().execute.return_value = {"files": []}
    return mock_service


class TestCreateDriveFileUploadId:
    @pytest.mark.asyncio
    async def test_invalid_upload_id_format_raises(self):
        """Non-UUID upload_id should raise an exception."""
        fn = _unwrap(create_drive_file)
        service = _make_mock_drive_service()
        with patch("gdrive.drive_tools.resolve_folder_id", new_callable=AsyncMock, return_value="root"):
            with pytest.raises(Exception, match="Invalid upload_id format"):
                await fn(
                    service,
                    user_google_email="test@example.com",
                    file_name="test.txt",
                    upload_id="not-a-uuid",
                )

    @pytest.mark.asyncio
    async def test_expired_upload_id_raises(self):
        """Expired/missing upload_id should raise an exception."""
        fn = _unwrap(create_drive_file)
        service = _make_mock_drive_service()
        fake_id = str(uuid.uuid4())
        with patch(
            "gdrive.drive_tools.get_attachment_storage"
        ) as mock_storage_fn:
            mock_storage = Mock()
            mock_storage.get_attachment_path.return_value = None
            mock_storage_fn.return_value = mock_storage

            with patch("gdrive.drive_tools.resolve_folder_id", new_callable=AsyncMock, return_value="root"):
                with pytest.raises(Exception, match="Upload not found or expired"):
                    await fn(
                        service,
                        user_google_email="test@example.com",
                        file_name="test.txt",
                        upload_id=fake_id,
                    )

    @pytest.mark.asyncio
    async def test_upload_id_reads_file_and_uploads(self):
        """Valid upload_id should read the file and upload to Drive."""
        fn = _unwrap(create_drive_file)
        service = _make_mock_drive_service()
        fake_id = str(uuid.uuid4())

        mock_path = MagicMock(spec=Path)
        mock_path.read_bytes.return_value = b"file content"

        with patch(
            "gdrive.drive_tools.get_attachment_storage"
        ) as mock_storage_fn:
            mock_storage = Mock()
            mock_storage.get_attachment_path.return_value = mock_path
            mock_storage.get_attachment_metadata.return_value = {
                "mime_type": "image/png",
                "filename": "photo.png",
            }
            mock_storage_fn.return_value = mock_storage

            with patch("gdrive.drive_tools.resolve_folder_id", new_callable=AsyncMock, return_value="root"):
                result = await fn(
                    service,
                    user_google_email="test@example.com",
                    file_name="photo.png",
                    upload_id=fake_id,
                )

            assert "file_123" in result
            assert "Successfully created file" in result

    @pytest.mark.asyncio
    async def test_upload_id_inherits_mime_type_from_upload(self):
        """When caller uses default mime_type, upload's stored mime_type should be used."""
        fn = _unwrap(create_drive_file)
        service = _make_mock_drive_service()
        fake_id = str(uuid.uuid4())

        mock_path = MagicMock(spec=Path)
        mock_path.read_bytes.return_value = b"png bytes"

        with patch(
            "gdrive.drive_tools.get_attachment_storage"
        ) as mock_storage_fn:
            mock_storage = Mock()
            mock_storage.get_attachment_path.return_value = mock_path
            mock_storage.get_attachment_metadata.return_value = {
                "mime_type": "image/png",
                "filename": "photo.png",
            }
            mock_storage_fn.return_value = mock_storage

            with patch("gdrive.drive_tools.resolve_folder_id", new_callable=AsyncMock, return_value="root"):
                await fn(
                    service,
                    user_google_email="test@example.com",
                    file_name="photo.png",
                    upload_id=fake_id,
                    mime_type="text/plain",  # default
                )

            # The create call should use the upload's mime type
            create_call = service.files().create
            call_kwargs = create_call.call_args
            body = call_kwargs.kwargs.get("body") or call_kwargs[1].get("body")
            assert body["mimeType"] == "image/png"

    @pytest.mark.asyncio
    async def test_upload_id_does_not_override_explicit_mime_type(self):
        """When caller provides explicit mime_type, upload's stored type should NOT override."""
        fn = _unwrap(create_drive_file)
        service = _make_mock_drive_service()
        fake_id = str(uuid.uuid4())

        mock_path = MagicMock(spec=Path)
        mock_path.read_bytes.return_value = b"pdf bytes"

        with patch(
            "gdrive.drive_tools.get_attachment_storage"
        ) as mock_storage_fn:
            mock_storage = Mock()
            mock_storage.get_attachment_path.return_value = mock_path
            mock_storage.get_attachment_metadata.return_value = {
                "mime_type": "image/png",
                "filename": "photo.png",
            }
            mock_storage_fn.return_value = mock_storage

            with patch("gdrive.drive_tools.resolve_folder_id", new_callable=AsyncMock, return_value="root"):
                await fn(
                    service,
                    user_google_email="test@example.com",
                    file_name="doc.pdf",
                    upload_id=fake_id,
                    mime_type="application/pdf",  # explicitly set
                )

            create_call = service.files().create
            call_kwargs = create_call.call_args
            body = call_kwargs.kwargs.get("body") or call_kwargs[1].get("body")
            assert body["mimeType"] == "application/pdf"

    @pytest.mark.asyncio
    async def test_upload_id_octet_stream_does_not_override(self):
        """application/octet-stream from upload should not override caller's default."""
        fn = _unwrap(create_drive_file)
        service = _make_mock_drive_service()
        fake_id = str(uuid.uuid4())

        mock_path = MagicMock(spec=Path)
        mock_path.read_bytes.return_value = b"data"

        with patch(
            "gdrive.drive_tools.get_attachment_storage"
        ) as mock_storage_fn:
            mock_storage = Mock()
            mock_storage.get_attachment_path.return_value = mock_path
            mock_storage.get_attachment_metadata.return_value = {
                "mime_type": "application/octet-stream",
            }
            mock_storage_fn.return_value = mock_storage

            with patch("gdrive.drive_tools.resolve_folder_id", new_callable=AsyncMock, return_value="root"):
                await fn(
                    service,
                    user_google_email="test@example.com",
                    file_name="file.bin",
                    upload_id=fake_id,
                    mime_type="text/plain",
                )

            create_call = service.files().create
            call_kwargs = create_call.call_args
            body = call_kwargs.kwargs.get("body") or call_kwargs[1].get("body")
            # Should keep text/plain, not override with octet-stream
            assert body["mimeType"] == "text/plain"


# ---------------------------------------------------------------------------
# create_drive_file with file_content_base64
# ---------------------------------------------------------------------------


class TestCreateDriveFileBase64:
    @pytest.mark.asyncio
    async def test_base64_decodes_and_uploads(self):
        """Valid base64 content should be decoded and uploaded."""
        fn = _unwrap(create_drive_file)
        service = _make_mock_drive_service()
        content = base64.b64encode(b"hello world").decode()

        with patch("gdrive.drive_tools.resolve_folder_id", new_callable=AsyncMock, return_value="root"):
            result = await fn(
                service,
                user_google_email="test@example.com",
                file_name="hello.txt",
                file_content_base64=content,
            )

        assert "Successfully created file" in result

    @pytest.mark.asyncio
    async def test_invalid_base64_raises(self):
        """Invalid base64 data should raise an exception."""
        fn = _unwrap(create_drive_file)
        service = _make_mock_drive_service()

        with patch("gdrive.drive_tools.resolve_folder_id", new_callable=AsyncMock, return_value="root"):
            with pytest.raises(Exception, match="Invalid base64 data"):
                await fn(
                    service,
                    user_google_email="test@example.com",
                    file_name="bad.txt",
                    file_content_base64="not!valid!base64!!!",
                )

    @pytest.mark.asyncio
    async def test_base64_uses_provided_mime_type(self):
        """Base64 upload should use the caller-provided mime_type."""
        fn = _unwrap(create_drive_file)
        service = _make_mock_drive_service()
        content = base64.b64encode(b"\x89PNG\r\n").decode()

        with patch("gdrive.drive_tools.resolve_folder_id", new_callable=AsyncMock, return_value="root"):
            await fn(
                service,
                user_google_email="test@example.com",
                file_name="image.png",
                file_content_base64=content,
                mime_type="image/png",
            )

        create_call = service.files().create
        call_kwargs = create_call.call_args
        body = call_kwargs.kwargs.get("body") or call_kwargs[1].get("body")
        assert body["mimeType"] == "image/png"

    @pytest.mark.asyncio
    async def test_base64_single_byte(self):
        """Minimal base64 content (single byte) should work."""
        fn = _unwrap(create_drive_file)
        service = _make_mock_drive_service()
        content = base64.b64encode(b"\x00").decode()

        with patch("gdrive.drive_tools.resolve_folder_id", new_callable=AsyncMock, return_value="root"):
            result = await fn(
                service,
                user_google_email="test@example.com",
                file_name="tiny.bin",
                file_content_base64=content,
            )

        assert "Successfully created file" in result


# ---------------------------------------------------------------------------
# create_drive_file validation
# ---------------------------------------------------------------------------


class TestCreateDriveFileValidation:
    @pytest.mark.asyncio
    async def test_no_content_source_raises(self):
        """No content, fileUrl, upload_id, or file_content_base64 should raise."""
        fn = _unwrap(create_drive_file)
        service = _make_mock_drive_service()
        with pytest.raises(
            Exception, match="You must provide either"
        ):
            await fn(
                service,
                user_google_email="test@example.com",
                file_name="test.txt",
            )

    @pytest.mark.asyncio
    async def test_upload_id_takes_priority_over_content(self):
        """upload_id should be used even if content is also provided."""
        fn = _unwrap(create_drive_file)
        service = _make_mock_drive_service()
        fake_id = str(uuid.uuid4())

        mock_path = MagicMock(spec=Path)
        mock_path.read_bytes.return_value = b"from upload"

        with patch(
            "gdrive.drive_tools.get_attachment_storage"
        ) as mock_storage_fn:
            mock_storage = Mock()
            mock_storage.get_attachment_path.return_value = mock_path
            mock_storage.get_attachment_metadata.return_value = None
            mock_storage_fn.return_value = mock_storage

            with patch("gdrive.drive_tools.resolve_folder_id", new_callable=AsyncMock, return_value="root"):
                result = await fn(
                    service,
                    user_google_email="test@example.com",
                    file_name="test.txt",
                    content="from content param",
                    upload_id=fake_id,
                )

            assert "Successfully created file" in result
            # read_bytes was called, meaning upload_id path was taken
            mock_path.read_bytes.assert_called_once()

    @pytest.mark.asyncio
    async def test_base64_takes_priority_over_fileUrl(self):
        """file_content_base64 should be used over fileUrl."""
        fn = _unwrap(create_drive_file)
        service = _make_mock_drive_service()
        content = base64.b64encode(b"base64 data").decode()

        with patch("gdrive.drive_tools.resolve_folder_id", new_callable=AsyncMock, return_value="root"):
            result = await fn(
                service,
                user_google_email="test@example.com",
                file_name="test.txt",
                file_content_base64=content,
                fileUrl="https://example.com/file.txt",
            )

        assert "Successfully created file" in result
        # No HTTP request should have been made since base64 takes priority
