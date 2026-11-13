"""The OAuth proxy's client redirect allow-list (SwarmSec #154) admits the first-party
LLM clients this server is for and nothing else, checked with FastMCP's own matcher
against the list the server actually passes (OAuthConfig.get_allowed_client_redirect_uris)."""

import os
import sys
from urllib.parse import urlparse

import pytest
from fastmcp.server.auth.redirect_validation import validate_redirect_uri

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from auth.oauth_config import DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS, OAuthConfig  # noqa: E402


@pytest.fixture
def allow_list(monkeypatch):
    monkeypatch.delenv("MCP_ALLOWED_CLIENT_REDIRECT_URIS", raising=False)
    return OAuthConfig().get_allowed_client_redirect_uris()


@pytest.mark.parametrize(
    "uri",
    [
        # Claude desktop app's sign-in callback.
        "claude://claude.ai/mcp-auth-callback/sdk",
        "http://localhost:5726/oauth/callback",
        "https://claude.ai/api/mcp/auth_callback",
    ],
)
def test_default_allow_list_admits_first_party_clients(allow_list, uri):
    assert validate_redirect_uri(uri, allow_list)


@pytest.mark.parametrize(
    "uri",
    [
        "claude://evil.example/mcp-auth-callback/sdk",
        "claude://claude.ai/other-path",
        "https://evil.example/callback",
        "http://localhost@evil.example/callback",
    ],
)
def test_default_allow_list_rejects_other_redirects(allow_list, uri):
    assert not validate_redirect_uri(uri, allow_list)


@pytest.mark.parametrize("pattern", DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS)
def test_every_pattern_names_a_host(pattern):
    # FastMCP compares hosts literally (only "*." subdomain wildcards), so a pattern
    # like "cursor://*" would silently match nothing.
    host = urlparse(pattern).hostname or ""
    assert host and host != "*" and "*" not in host.removeprefix("*."), pattern
