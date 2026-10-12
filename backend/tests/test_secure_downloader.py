"""
Unit tests for SecurePDFDownloader covering URL validation, SSRF defenses,
streaming bounds, magic byte verification, redirect handling, and cleanup.
All tests use mock transports or local validation; zero internet requests are made.
"""
import hashlib
import ipaddress
from pathlib import Path
import tempfile
import pytest
import httpx

from app.ingestion.downloader import (
    SecurePDFDownloader,
    DownloadResult,
    DownloadSecurityError,
    SSRFValidationError,
    InvalidPDFError,
    DownloadLimitExceededError,
    DownloadHTTPError,
    is_ip_allowed,
)


# Minimal valid PDF file bytes for test mocks
VALID_PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<\n/Type /Catalog\n>>\nendobj\ntrailer\n<<\n>>\n%%EOF\n"
VALID_PDF_SHA256 = hashlib.sha256(VALID_PDF_BYTES).hexdigest()


class TestIPAndURLValidation:
    """Tests for SSRF IP checks and URL structure validation."""

    def test_ip_allow_and_block_rules(self):
        # Public IP allowed
        allowed, _ = is_ip_allowed("93.184.216.34")
        assert allowed is True

        # Private IPv4 blocked
        allowed, reason = is_ip_allowed("10.0.0.1")
        assert allowed is False
        assert "Private" in reason

        allowed, reason = is_ip_allowed("192.168.1.1")
        assert allowed is False
        assert "Private" in reason

        allowed, reason = is_ip_allowed("172.16.0.5")
        assert allowed is False
        assert "Private" in reason

        # Loopback IPv4 blocked
        allowed, reason = is_ip_allowed("127.0.0.1")
        assert allowed is False
        assert "Loopback" in reason

        # Loopback permitted when test flag is True
        allowed, reason = is_ip_allowed("127.0.0.1", allow_test_loopback=True)
        assert allowed is True

        # Link-local / cloud metadata (169.254.169.254) blocked
        allowed, reason = is_ip_allowed("169.254.169.254")
        assert allowed is False
        assert "Link-local" in reason

        # IPv6 loopback blocked
        allowed, reason = is_ip_allowed("::1")
        assert allowed is False
        assert "Loopback" in reason

        # IPv4-mapped IPv6 loopback (::ffff:127.0.0.1) blocked
        allowed, reason = is_ip_allowed("::ffff:127.0.0.1")
        assert allowed is False
        assert "Loopback" in reason

        # IPv4-mapped IPv6 private (::ffff:10.0.0.1) blocked
        allowed, reason = is_ip_allowed("::ffff:10.0.0.1")
        assert allowed is False
        assert "Private" in reason

        # Multicast and reserved
        allowed, reason = is_ip_allowed("224.0.0.1")
        assert allowed is False

    def test_url_scheme_validation(self):
        downloader = SecurePDFDownloader(allow_http_for_test=False)

        # HTTPS is accepted
        scheme, host, port = downloader.validate_url("https://www.sebi.gov.in/circular.pdf")
        assert scheme == "https"
        assert host == "www.sebi.gov.in"
        assert port == 443

        # HTTP rejected in default production mode
        with pytest.raises(SSRFValidationError, match="Insecure HTTP scheme"):
            downloader.validate_url("http://www.sebi.gov.in/circular.pdf")

        # Unsupported schemes rejected
        with pytest.raises(SSRFValidationError, match="Unsupported URL scheme"):
            downloader.validate_url("file:///etc/passwd")

        with pytest.raises(SSRFValidationError, match="Unsupported URL scheme"):
            downloader.validate_url("ftp://sebi.gov.in/circular.pdf")

        with pytest.raises(SSRFValidationError, match="Unsupported URL scheme"):
            downloader.validate_url("gopher://sebi.gov.in/payload")

    def test_http_allowed_only_in_explicit_test_mode(self):
        test_downloader = SecurePDFDownloader(allow_http_for_test=True)
        scheme, host, port = test_downloader.validate_url("http://127.0.0.1:8000/mock.pdf")
        assert scheme == "http"
        assert host == "127.0.0.1"
        assert port == 8000

    def test_url_credentials_rejected(self):
        downloader = SecurePDFDownloader(allow_http_for_test=True)
        with pytest.raises(SSRFValidationError, match="User credentials"):
            downloader.validate_url("https://admin:secret@sebi.gov.in/doc.pdf")

    def test_disallowed_ports_rejected(self):
        downloader = SecurePDFDownloader(allow_http_for_test=False)
        with pytest.raises(SSRFValidationError, match="Disallowed connection port"):
            downloader.validate_url("https://sebi.gov.in:22/doc.pdf")


class TestPDFDownloadStreamingAndValidation:
    """Tests using httpx.MockTransport to test streaming, magic bytes, size limits, and cleanup."""

    def test_valid_pdf_download_and_sha256(self):
        # Mock successful 200 response with valid PDF
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                headers={"Content-Type": "application/pdf"},
                content=VALID_PDF_BYTES,
            )

        mock_transport = httpx.MockTransport(handler)
        with tempfile.TemporaryDirectory() as staging_dir:
            downloader = SecurePDFDownloader(
                allow_http_for_test=True,
                staging_dir=Path(staging_dir),
                custom_transport=mock_transport,
            )
            result = downloader.fetch_pdf("http://127.0.0.1:8000/valid.pdf")

            assert result.byte_count == len(VALID_PDF_BYTES)
            assert result.sha256 == VALID_PDF_SHA256
            assert result.staging_path.exists()
            assert result.staging_path.read_bytes() == VALID_PDF_BYTES

    def test_reject_html_payload(self):
        # Mock 200 response returning HTML (e.g. 404 page or login page)
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                headers={"Content-Type": "text/html"},
                content=b"<!DOCTYPE html><html><body>Login Required</body></html>",
            )

        mock_transport = httpx.MockTransport(handler)
        downloader = SecurePDFDownloader(
            allow_http_for_test=True,
            custom_transport=mock_transport,
        )
        with pytest.raises(InvalidPDFError, match="Received HTML payload"):
            downloader.fetch_pdf("http://127.0.0.1:8000/login.html")

    def test_reject_magic_bytes_mismatch(self):
        # Content-Type says application/pdf, but bytes are plain text
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                headers={"Content-Type": "application/pdf"},
                content=b"This is a fake text file posing as a PDF",
            )

        mock_transport = httpx.MockTransport(handler)
        downloader = SecurePDFDownloader(
            allow_http_for_test=True,
            custom_transport=mock_transport,
        )
        with pytest.raises(InvalidPDFError, match="header does not begin with '%PDF-'"):
            downloader.fetch_pdf("http://127.0.0.1:8000/fake.pdf")

    def test_reject_empty_payload(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                headers={"Content-Type": "application/pdf"},
                content=b"",
            )

        mock_transport = httpx.MockTransport(handler)
        downloader = SecurePDFDownloader(
            allow_http_for_test=True,
            custom_transport=mock_transport,
        )
        with pytest.raises(InvalidPDFError, match="Downloaded file is 0 bytes"):
            downloader.fetch_pdf("http://127.0.0.1:8000/empty.pdf")

    def test_enforce_maximum_download_size_during_stream(self):
        # Stream 500 KB when limit is set to 100 KB
        oversized_data = VALID_PDF_BYTES + (b"A" * 500000)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                headers={"Content-Type": "application/pdf"},
                content=oversized_data,
            )

        mock_transport = httpx.MockTransport(handler)
        with tempfile.TemporaryDirectory() as staging_dir:
            staging_path = Path(staging_dir)
            downloader = SecurePDFDownloader(
                max_bytes=100000,  # 100 KB limit
                allow_http_for_test=True,
                staging_dir=staging_path,
                custom_transport=mock_transport,
            )
            with pytest.raises(DownloadLimitExceededError, match="exceeded maximum size limit"):
                downloader.fetch_pdf("http://127.0.0.1:8000/oversized.pdf")

            # Verify that partial staging file was deleted
            staging_files = list(staging_path.glob("*.staging"))
            assert len(staging_files) == 0

    def test_http_error_status_handling(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=404, content=b"Not Found")

        mock_transport = httpx.MockTransport(handler)
        downloader = SecurePDFDownloader(
            allow_http_for_test=True,
            custom_transport=mock_transport,
        )
        with pytest.raises(DownloadHTTPError, match="HTTP 404"):
            downloader.fetch_pdf("http://127.0.0.1:8000/missing.pdf")

    def test_safe_redirect_handling_and_revalidation(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/initial.pdf":
                return httpx.Response(
                    status_code=302,
                    headers={"Location": "/final.pdf"},
                )
            elif request.url.path == "/final.pdf":
                return httpx.Response(
                    status_code=200,
                    headers={"Content-Type": "application/pdf"},
                    content=VALID_PDF_BYTES,
                )
            return httpx.Response(status_code=404)

        mock_transport = httpx.MockTransport(handler)
        downloader = SecurePDFDownloader(
            allow_http_for_test=True,
            custom_transport=mock_transport,
        )
        result = downloader.fetch_pdf("http://127.0.0.1:8000/initial.pdf")
        assert result.final_url == "http://127.0.0.1:8000/final.pdf"
        assert result.byte_count == len(VALID_PDF_BYTES)

    def test_redirect_loop_detected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/loop_a":
                return httpx.Response(status_code=302, headers={"Location": "/loop_b"})
            return httpx.Response(status_code=302, headers={"Location": "/loop_a"})

        mock_transport = httpx.MockTransport(handler)
        downloader = SecurePDFDownloader(
            allow_http_for_test=True,
            custom_transport=mock_transport,
        )
        with pytest.raises(SSRFValidationError, match="Redirect loop detected"):
            downloader.fetch_pdf("http://127.0.0.1:8000/loop_a")

    def test_exceeded_maximum_redirects(self):
        def handler(request: httpx.Request) -> httpx.Response:
            step = int(request.url.path.replace("/step", "") or "0")
            return httpx.Response(status_code=302, headers={"Location": f"/step{step + 1}"})

        mock_transport = httpx.MockTransport(handler)
        downloader = SecurePDFDownloader(
            max_redirects=2,
            allow_http_for_test=True,
            custom_transport=mock_transport,
        )
        with pytest.raises(SSRFValidationError, match="Exceeded maximum allowed redirects"):
            downloader.fetch_pdf("http://127.0.0.1:8000/step0")

    def test_redirect_to_disallowed_scheme_blocked(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=302, headers={"Location": "file:///etc/shadow"})

        mock_transport = httpx.MockTransport(handler)
        downloader = SecurePDFDownloader(
            allow_http_for_test=True,
            custom_transport=mock_transport,
        )
        with pytest.raises(SSRFValidationError, match="Unsupported URL scheme"):
            downloader.fetch_pdf("http://127.0.0.1:8000/redirect_file")
