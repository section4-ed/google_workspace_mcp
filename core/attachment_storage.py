"""
Temporary attachment storage for Gmail attachments.

Stores attachments to local disk and returns file paths for direct access.
Files are automatically cleaned up after expiration (default 1 hour).
"""

import base64
import logging
import os
import secrets
import uuid
from pathlib import Path
from typing import Callable, NamedTuple, Optional, Dict, Tuple
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Default expiration: 1 hour
DEFAULT_EXPIRATION_SECONDS = 3600

# Single-use upload tokens minted by the create_upload_url tool.
UPLOAD_TOKEN_TTL_SECONDS = 600

# Storage directory - configurable via WORKSPACE_ATTACHMENT_DIR env var
# Uses absolute path to avoid creating tmp/ in arbitrary working directories (see #327)
_default_dir = str(Path.home() / ".workspace-mcp" / "attachments")
STORAGE_DIR = (
    Path(os.getenv("WORKSPACE_ATTACHMENT_DIR", _default_dir)).expanduser().resolve()
)
# Private POST /upload files live apart so validate_file_path can block them.
UPLOADS_DIR = STORAGE_DIR / "uploads"


def _ensure_dir(directory: Path) -> None:
    """Create a storage directory on first use, not at import time."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)


class SavedAttachment(NamedTuple):
    """Result of saving an attachment: provides both the UUID and the absolute file path."""

    file_id: str
    path: str


class AttachmentStorage:
    """Manages temporary storage of email attachments."""

    def __init__(self, expiration_seconds: int = DEFAULT_EXPIRATION_SECONDS):
        self.expiration_seconds = expiration_seconds
        self._metadata: Dict[str, Dict] = {}

    def save_bytes(
        self,
        file_bytes: bytes,
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
        owner_email: Optional[str] = None,
    ) -> SavedAttachment:
        """
        Save raw bytes to local disk.

        Args:
            file_bytes: Raw file bytes
            filename: Original filename (optional)
            mime_type: MIME type (optional)
            owner_email: Owner of a private upload; owned files are never served (optional)

        Returns:
            SavedAttachment with file_id (UUID) and path (absolute file path)
        """
        return self._save(file_bytes, filename, mime_type, owner_email=owner_email)

    def save_attachment(
        self,
        base64_data: str,
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> SavedAttachment:
        """
        Save an attachment to local disk.

        Args:
            base64_data: Base64-encoded attachment data
            filename: Original filename (optional)
            mime_type: MIME type (optional)

        Returns:
            SavedAttachment with file_id (UUID) and path (absolute file path)
        """
        # Decode base64 data
        try:
            file_bytes = base64.urlsafe_b64decode(base64_data)
        except Exception as e:
            logger.error(f"Failed to decode base64 attachment data: {e}")
            raise ValueError(f"Invalid base64 data: {e}")

        return self._save(file_bytes, filename, mime_type)

    def _save(
        self,
        file_bytes: bytes,
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
        owner_email: Optional[str] = None,
    ) -> SavedAttachment:
        """Internal helper to save bytes to disk."""
        target_dir = UPLOADS_DIR if owner_email else STORAGE_DIR
        _ensure_dir(target_dir)

        # Generate unique file ID for metadata tracking
        file_id = str(uuid.uuid4())

        # Determine file extension from filename or mime type
        extension = ""
        if filename:
            extension = Path(filename).suffix
        elif mime_type:
            # Basic mime type to extension mapping
            mime_to_ext = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/gif": ".gif",
                "application/pdf": ".pdf",
                "application/zip": ".zip",
                "text/plain": ".txt",
                "text/html": ".html",
            }
            extension = mime_to_ext.get(mime_type, "")

        # Use original filename if available, with UUID suffix for uniqueness.
        # Sanitize to prevent path traversal: extract only the final component.
        if filename:
            safe = Path(Path(filename).name)  # strips any directory components
            save_name = f"{safe.stem}_{file_id[:8]}{safe.suffix}"
        else:
            save_name = f"{file_id}{extension}"

        # Save file with restrictive permissions (sensitive email/drive content)
        file_path = (target_dir / save_name).resolve()
        if not file_path.is_relative_to(target_dir):
            raise ValueError("Invalid filename: path escapes storage directory")
        try:
            fd = os.open(
                file_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0),
                0o600,
            )
            try:
                total_written = 0
                data_len = len(file_bytes)
                while total_written < data_len:
                    written = os.write(fd, file_bytes[total_written:])
                    if written == 0:
                        raise OSError(
                            "os.write returned 0 bytes; could not write attachment data"
                        )
                    total_written += written
            finally:
                os.close(fd)
            logger.info(f"Saved attachment file_id={file_id} ({len(file_bytes)} bytes)")
        except Exception as e:
            logger.error(f"Failed to save attachment file_id={file_id}: {e}")
            raise

        # Store metadata
        now = datetime.now()
        self._metadata[file_id] = {
            "file_path": str(file_path),
            "filename": filename or f"attachment{extension}",
            "mime_type": mime_type or "application/octet-stream",
            "size": len(file_bytes),
            "owner_email": owner_email.lower() if owner_email else None,
            "created_at": now,
            "expires_at": now + timedelta(seconds=self.expiration_seconds),
        }

        return SavedAttachment(file_id=file_id, path=str(file_path))

    def get_attachment_metadata(self, file_id: str) -> Optional[Dict]:
        """
        Get metadata for an attachment.

        Args:
            file_id: Unique file ID

        Returns:
            Metadata dict if exists and not expired, None otherwise
        """
        if file_id not in self._metadata:
            return None

        metadata = self._metadata[file_id].copy()

        # Check if expired
        if datetime.now() > metadata["expires_at"]:
            self.delete(file_id)
            return None

        return metadata

    def get_servable(self, file_id: str) -> Optional[Tuple[Path, Dict]]:
        """Path and metadata for /attachments, or None.

        Owned files (POST /upload) are private and never served.
        """
        return self._resolve(file_id, lambda owner: not owner)

    def get_owned_upload(
        self, file_id: str, owner_email: str
    ) -> Optional[Tuple[Path, Dict]]:
        """Path and metadata of an upload owned by owner_email, or None."""
        if not owner_email:
            return None
        return self._resolve(file_id, lambda owner: owner == owner_email.lower())

    def claim_owned_upload(
        self, file_id: str, owner_email: str
    ) -> Optional[Tuple[Path, Dict]]:
        """Take an owned upload out of the store so no concurrent call can use it.

        The file stays on disk; call release() to put it back if the caller fails.
        """
        owned = self.get_owned_upload(file_id, owner_email)
        if owned is not None:
            del self._metadata[file_id]
        return owned

    def release(self, file_id: str, metadata: Dict) -> None:
        """Return a claimed upload to the store for a retry."""
        self._metadata[file_id] = metadata

    def _resolve(
        self, file_id: str, owner_ok: Callable[[Optional[str]], bool]
    ) -> Optional[Tuple[Path, Dict]]:
        metadata = self.get_attachment_metadata(file_id)
        if not metadata or not owner_ok(metadata.get("owner_email")):
            return None
        path = Path(metadata["file_path"])
        return (path, metadata) if path.exists() else None

    def delete(self, file_id: str) -> None:
        """Remove file and metadata."""
        if file_id in self._metadata:
            file_path = Path(self._metadata[file_id]["file_path"])
            try:
                if file_path.exists():
                    file_path.unlink()
                    logger.debug(f"Deleted expired attachment file: {file_path}")
            except Exception as e:
                logger.warning(f"Failed to delete attachment file {file_path}: {e}")
            del self._metadata[file_id]

    def cleanup_expired(self) -> int:
        """
        Clean up expired attachments.

        Returns:
            Number of files cleaned up
        """
        now = datetime.now()
        expired_ids = [
            file_id
            for file_id, metadata in self._metadata.items()
            if now > metadata["expires_at"]
        ]

        for file_id in expired_ids:
            self.delete(file_id)

        return len(expired_ids)


class UploadTokenStore:
    """Single-use, short-lived tokens, minted by the authenticated
    create_upload_url tool, that bind one POST /upload to the caller's email."""

    def __init__(self, ttl_seconds: int = UPLOAD_TOKEN_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._tokens: Dict[str, Tuple[str, datetime]] = {}

    def issue(self, owner_email: str) -> Tuple[str, datetime]:
        """Mint a token for owner_email and return it with its expiry."""
        if not owner_email:
            raise ValueError("owner_email is required to issue an upload token")
        self._prune()
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now() + timedelta(seconds=self.ttl_seconds)
        self._tokens[token] = (owner_email.lower(), expires_at)
        return token, expires_at

    def consume(self, token: Optional[str]) -> Optional[str]:
        """Return the token's owner and invalidate it, or None if not valid."""
        entry = self._tokens.pop(token, None)
        if entry is None:
            return None
        owner_email, expires_at = entry
        if datetime.now() > expires_at:
            return None
        return owner_email

    def _prune(self) -> None:
        now = datetime.now()
        for token in [t for t, (_, exp) in self._tokens.items() if now > exp]:
            del self._tokens[token]


# Global instances
_attachment_storage: Optional[AttachmentStorage] = None
_upload_token_store: Optional[UploadTokenStore] = None


def get_attachment_storage() -> AttachmentStorage:
    """Get the global attachment storage instance."""
    global _attachment_storage
    if _attachment_storage is None:
        _attachment_storage = AttachmentStorage()
    return _attachment_storage


def get_upload_token_store() -> UploadTokenStore:
    """Get the global upload token store."""
    global _upload_token_store
    if _upload_token_store is None:
        _upload_token_store = UploadTokenStore()
    return _upload_token_store


def _external_base_url() -> str:
    """Base URL clients use to reach this server."""
    from core.config import WORKSPACE_MCP_PORT, WORKSPACE_MCP_BASE_URI

    # Use external URL if set (for reverse proxy scenarios)
    external_url = os.getenv("WORKSPACE_EXTERNAL_URL")
    if external_url:
        return external_url.rstrip("/")
    return f"{WORKSPACE_MCP_BASE_URI}:{WORKSPACE_MCP_PORT}"


def get_upload_url(token: str) -> str:
    """URL a client POSTs one file to, authorized by a single-use token."""
    return f"{_external_base_url()}/upload?token={token}"


def get_attachment_url(file_id: str) -> str:
    """
    Generate a URL for accessing an attachment.

    Args:
        file_id: Unique file ID

    Returns:
        Full URL to access the attachment
    """
    return f"{_external_base_url()}/attachments/{file_id}"
