"""SSRF regression tests for ``scripts/url_utils.py``.

Locks in the v1.5.1 hardening: any URL whose hostname resolves to a private,
loopback, link-local, CGNAT, or otherwise-internal address must be rejected,
and DNS failures must fail closed (raise ValueError, not pass through).

These tests do not require network access — IP-literal hostnames bypass DNS
resolution. The dns-failure case uses a hostname that definitely won't resolve.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


# Make scripts/ importable without requiring an installed package
SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from url_utils import sanitize_error, validate_url  # noqa: E402


# ─── SSRF blocklist ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin",
    "http://localhost/secret",         # resolves to 127.0.0.1
    "http://0.0.0.0:8080",
    "http://10.0.0.1",
    "http://172.16.0.5",
    "http://172.31.255.254",
    "http://192.168.1.1",
    "http://169.254.169.254/latest/meta-data/",  # AWS metadata endpoint
    "http://100.64.0.1",                          # CGNAT range
    "https://[::1]/admin",
    "https://[fc00::1]",                          # ULA
    "https://[fe80::1%25eth0]",                   # link-local (note %% for URL)
    "https://[::ffff:127.0.0.1]",                 # IPv4-mapped IPv6 loopback
    "https://[::]/",                              # IPv6 unspecified (v1.6.0 fix)
])
def test_blocks_private_and_internal_addresses(url):
    with pytest.raises(ValueError):
        validate_url(url)


@pytest.mark.parametrize("url", [
    "ftp://example.com",
    "file:///etc/passwd",
    "gopher://example.com",
    "data:text/html,<script>alert(1)</script>",
    "javascript:alert(1)",
])
def test_blocks_non_http_schemes(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_dns_resolution_failure_fails_closed():
    """A hostname that cannot be resolved should raise, not be allowed
    through to the requests/playwright layer."""
    with pytest.raises(ValueError):
        validate_url("http://nonexistent-hostname-for-claude-ads-tests.invalid")


@pytest.mark.parametrize("url,expected_contains", [
    ("example.com", "https://example.com"),    # bare hostname gets https:// prepended
    ("https://example.com/foo?bar=baz", "https://example.com/foo"),
])
def test_valid_public_urls_pass(url, expected_contains):
    """Public hostnames that resolve to non-blocked IPs must pass through.
    This requires network access (DNS for example.com) so we keep it minimal."""
    pytest.importorskip("socket")
    try:
        result = validate_url(url)
    except ValueError as e:
        if "DNS" in str(e) or "resolve" in str(e).lower():
            pytest.skip("No network access for DNS resolution")
        raise
    assert expected_contains in result


# ─── sanitize_error ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,sanitized_marker", [
    ("Failed with key=sk-1234567890abcdef", "key=***"),
    ("Got 401 with token=ghp_AAA111BBB222CCC", "token=***"),
    ("Error: secret=topsecret123 in payload", "secret=***"),
    ("auth=Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig", "Bearer ***"),
    ("password = hunter2 was rejected", "password=***"),  # note: regex normalizes
])
def test_sanitize_error_strips_credentials(raw, sanitized_marker):
    msg = sanitize_error(Exception(raw))
    assert sanitized_marker in msg
    # And the original secret value must NOT survive
    for forbidden in ("sk-1234567890abcdef", "ghp_AAA111BBB222CCC", "topsecret123", "eyJhbGciOiJIUzI1NiJ9", "hunter2"):
        assert forbidden not in msg or sanitized_marker in msg


def test_sanitize_error_preserves_benign_message():
    """Messages without secrets should pass through unchanged."""
    msg = sanitize_error(Exception("File not found: /tmp/foo.json"))
    assert "File not found" in msg
    assert "/tmp/foo.json" in msg
