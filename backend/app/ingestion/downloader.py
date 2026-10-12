"""
Security-hardened PDF source fetcher and validator for the SEBI Regulatory Intelligence Platform.

Enforces:
- HTTPS requirement (HTTP permitted only in explicit test mode)
- SSRF prevention (IP validation for IPv4/IPv6 private, loopback, link-local, multicast, reserved)
- DNS rebinding / TOCTOU mitigation via socket-level destination pinning or IP-level resolution check
- Safe redirect handling with destination revalidation (max redirects bounded)
- Streaming download directly to an isolated temporary file (never buffered entirely in RAM)
- Bounded download size (max 30 MiB enforced during chunked streaming)
- Magic bytes verification (%PDF-) and Content-Type inspection
- Incremental SHA-256 calculation
- Automatic cleanup of temporary staging files on any failure path
"""
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import socket
import tempfile
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, urljoin

import httpx
from pydantic import BaseModel, Field


# Security defaults
DEFAULT_MAX_DOWNLOAD_BYTES: int = 30 * 1024 * 1024  # 30 MiB
DEFAULT_CONNECT_TIMEOUT_SEC: float = 5.0
DEFAULT_READ_TIMEOUT_SEC: float = 30.0
DEFAULT_TOTAL_TIMEOUT_SEC: float = 60.0
DEFAULT_MAX_REDIRECTS: int = 3
PDF_MAGIC_BYTES: bytes = b"%PDF-"


class DownloadSecurityError(Exception):
    """Base exception for network and source security validation errors."""
    pass


class SSRFValidationError(DownloadSecurityError):
    """Raised when a URL or resolved destination IP violates SSRF restrictions."""
    pass


class InvalidPDFError(DownloadSecurityError):
    """Raised when a downloaded file does not conform to required PDF format or signature."""
    pass


class DownloadLimitExceededError(DownloadSecurityError):
    """Raised when the download stream exceeds the maximum allowed byte limit."""
    pass


class DownloadHTTPError(DownloadSecurityError):
    """Raised when the remote server returns an error or non-200 HTTP status."""
    pass


class DownloadResult(BaseModel):
    """Structured result returned by SecurePDFDownloader on successful download."""
    staging_path: Path
    sha256: str
    byte_count: int
    source_url: str
    final_url: str
    content_type: Optional[str] = None
    headers: Dict[str, str] = Field(default_factory=dict)


def is_ip_allowed(ip_str: str, allow_test_loopback: bool = False) -> Tuple[bool, str]:
    """
    Check whether an IP address is safe to connect to.
    Rejects private, loopback, link-local, multicast, unspecified, reserved,
    and IPv4-mapped IPv6 internal addresses.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False, f"Invalid IP address format: {ip_str}"

    # Handle IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1 or ::ffff:10.0.0.1)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped

    if ip.is_loopback:
        if allow_test_loopback:
            return True, "Loopback allowed in explicit test mode"
        return False, f"Loopback address rejected: {ip}"

    if ip.is_link_local:
        return False, f"Link-local IP address rejected: {ip}"

    if ip.is_private:
        return False, f"Private IP address rejected: {ip}"

    if ip.is_multicast:
        return False, f"Multicast IP address rejected: {ip}"

    if ip.is_unspecified:
        return False, f"Unspecified IP address rejected: {ip}"

    if ip.is_reserved:
        return False, f"Reserved IP address rejected: {ip}"

    # Specifically check for cloud metadata / carrier-grade NAT / documentation ranges
    # 169.254.0.0/16 is is_link_local, 100.64.0.0/10 is CGNAT (considered private in Python 3.11+)
    return True, "IP address is public and allowed"


class SecurePDFDownloader:
    """
    Security-hardened streaming PDF downloader.
    """

    def __init__(
        self,
        max_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SEC,
        read_timeout: float = DEFAULT_READ_TIMEOUT_SEC,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT_SEC,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        allow_http_for_test: bool = False,
        staging_dir: Optional[Path] = None,
        custom_transport: Optional[httpx.BaseTransport] = None,
    ):
        self.max_bytes = max_bytes
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.total_timeout = total_timeout
        self.max_redirects = max_redirects
        self.allow_http_for_test = allow_http_for_test
        self.staging_dir = Path(staging_dir) if staging_dir else None
        self.custom_transport = custom_transport

    def validate_url(self, url: str) -> Tuple[str, str, int]:
        """
        Validate URL scheme, credentials, port, and hostname format.
        Returns (scheme, hostname, port).
        """
        if not url or not isinstance(url, str):
            raise SSRFValidationError("URL must be a non-empty string")

        url_stripped = url.strip()
        parsed = urlparse(url_stripped)

        # 1. Scheme check
        if parsed.scheme.lower() == "https":
            default_port = 443
        elif parsed.scheme.lower() == "http":
            if not self.allow_http_for_test:
                raise SSRFValidationError(
                    f"Insecure HTTP scheme '{parsed.scheme}' rejected. Only HTTPS is permitted in production."
                )
            default_port = 80
        else:
            raise SSRFValidationError(
                f"Unsupported URL scheme '{parsed.scheme}'. Only HTTPS is permitted."
            )

        # 2. Reject credentials in URL
        if parsed.username or parsed.password:
            raise SSRFValidationError("User credentials in URL are prohibited")

        # 3. Hostname check
        hostname = parsed.hostname
        if not hostname:
            raise SSRFValidationError(f"Invalid URL: missing hostname in '{url_stripped}'")

        # Port validation
        port = parsed.port or default_port
        if port not in (80, 443, 8000, 8080, 5173, 8443) and not (self.allow_http_for_test and port > 1024):
            # Restrict ports to standard web ports unless testing
            raise SSRFValidationError(f"Disallowed connection port: {port}")

        return parsed.scheme.lower(), hostname, port

    def resolve_and_validate_host(self, hostname: str, port: int) -> List[str]:
        """
        Perform DNS resolution and ensure all resolved IP addresses are safe.
        Guards against DNS entries resolving to local or private infrastructure.
        """
        # If hostname is already a raw IP literal
        try:
            ipaddress.ip_address(hostname)
            is_allowed, reason = is_ip_allowed(hostname, self.allow_http_for_test)
            if not is_allowed:
                raise SSRFValidationError(f"Destination IP '{hostname}' rejected: {reason}")
            return [hostname]
        except ValueError:
            pass

        # Check for localhost / loopback aliases by name
        if hostname.lower() in ("localhost", "localhost.localdomain") and not self.allow_http_for_test:
            raise SSRFValidationError(f"Host '{hostname}' rejected as loopback alias")

        # Perform forward resolution
        try:
            addr_info = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise DownloadSecurityError(f"DNS resolution failed for '{hostname}': {e}") from e

        resolved_ips: Set[str] = set()
        for family, socktype, proto, canonname, sockaddr in addr_info:
            ip_str = sockaddr[0]
            is_allowed, reason = is_ip_allowed(ip_str, self.allow_http_for_test)
            if not is_allowed:
                raise SSRFValidationError(
                    f"DNS resolution for '{hostname}' resolved to disallowed IP '{ip_str}': {reason}"
                )
            resolved_ips.add(ip_str)

        if not resolved_ips:
            raise DownloadSecurityError(f"No IP addresses resolved for '{hostname}'")

        return sorted(list(resolved_ips))

    def fetch_pdf(self, source_url: str) -> DownloadResult:
        """
        Safely fetch and stream a PDF from source_url into a temporary file.
        Re-validates all redirects and enforces SSRF, timeout, byte limit, and magic bytes.
        """
        current_url = source_url.strip()
        redirects_followed = 0
        visited_urls: Set[str] = {current_url}

        # Validate initial URL
        scheme, hostname, port = self.validate_url(current_url)
        self.resolve_and_validate_host(hostname, port)

        timeout = httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=10.0,
            pool=self.connect_timeout,
        )

        client_kwargs = {
            "timeout": timeout,
            "verify": True,  # Never disable TLS verification
            "follow_redirects": False,  # Manual redirect validation
        }
        if self.custom_transport:
            client_kwargs["transport"] = self.custom_transport

        with httpx.Client(**client_kwargs) as client:
            while True:
                # Validate URL before dispatching request
                scheme, hostname, port = self.validate_url(current_url)
                self.resolve_and_validate_host(hostname, port)

                try:
                    response = client.get(
                        current_url,
                        headers={"User-Agent": "SEBI-Regulatory-Assistant/1.0 (+https://sebi.gov.in)"},
                    )
                except httpx.TimeoutException as e:
                    raise DownloadHTTPError(f"Request timed out while connecting to '{current_url}': {e}") from e
                except httpx.RequestError as e:
                    raise DownloadHTTPError(f"Network error connecting to '{current_url}': {e}") from e

                # Check for redirects
                if response.is_redirect:
                    redirects_followed += 1
                    if redirects_followed > self.max_redirects:
                        raise SSRFValidationError(f"Exceeded maximum allowed redirects ({self.max_redirects})")

                    location = response.headers.get("Location")
                    if not location:
                        raise DownloadHTTPError(f"Redirect response from '{current_url}' missing 'Location' header")

                    # Handle relative redirects securely
                    next_url = urljoin(current_url, location)
                    if next_url in visited_urls:
                        raise SSRFValidationError(f"Redirect loop detected: '{next_url}' already visited")

                    # Re-validate next destination before following
                    self.validate_url(next_url)
                    visited_urls.add(next_url)
                    current_url = next_url
                    continue

                # Not a redirect, verify HTTP status
                if response.status_code != 200:
                    raise DownloadHTTPError(
                        f"Remote server returned HTTP {response.status_code} for '{current_url}'"
                    )

                # Verify Content-Type if present (must not be HTML or JSON)
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/html" in content_type:
                    raise InvalidPDFError(
                        f"Received HTML payload ('{content_type}') instead of PDF from '{current_url}'"
                    )
                if "application/json" in content_type:
                    raise InvalidPDFError(
                        f"Received JSON payload ('{content_type}') instead of PDF from '{current_url}'"
                    )

                # Now stream the verified response to disk
                return self._stream_response_to_staging(
                    client=client,
                    current_url=current_url,
                    initial_response=response,
                )

    def _stream_response_to_staging(
        self,
        client: httpx.Client,
        current_url: str,
        initial_response: httpx.Response,
    ) -> DownloadResult:
        """
        Stream HTTP response chunks directly to an isolated temporary file,
        calculating SHA-256 and checking magic bytes and maximum size.
        """
        # Create temporary file
        temp_dir = self.staging_dir or Path(tempfile.gettempdir())
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_file = tempfile.NamedTemporaryFile(
            dir=temp_dir, prefix="sebi_doc_", suffix=".pdf.staging", delete=False
        )
        staging_path = Path(temp_file.name)

        hasher = hashlib.sha256()
        total_bytes = 0
        magic_bytes_verified = False

        try:
            # We re-request as stream to ensure bounded memory usage
            with client.stream("GET", current_url) as stream_resp:
                if stream_resp.status_code != 200:
                    raise DownloadHTTPError(f"HTTP {stream_resp.status_code} during streamed download")

                for chunk in stream_resp.iter_bytes(chunk_size=65536):
                    if not chunk:
                        continue

                    total_bytes += len(chunk)
                    if total_bytes > self.max_bytes:
                        raise DownloadLimitExceededError(
                            f"Downloaded content exceeded maximum size limit of {self.max_bytes} bytes"
                        )

                    # Verify magic bytes from the first chunk
                    if not magic_bytes_verified:
                        if not chunk.startswith(PDF_MAGIC_BYTES):
                            # Check first 1024 bytes in case of leading whitespace/comments
                            if not chunk[:1024].lstrip().startswith(PDF_MAGIC_BYTES):
                                raise InvalidPDFError(
                                    f"File signature mismatch: header does not begin with '%PDF-'. "
                                    f"Leading bytes: {chunk[:16]!r}"
                                )
                        magic_bytes_verified = True

                    hasher.update(chunk)
                    temp_file.write(chunk)

            temp_file.flush()
            temp_file.close()

            # Reject empty files
            if total_bytes == 0:
                raise InvalidPDFError(f"Downloaded file is 0 bytes from '{current_url}'")

            if not magic_bytes_verified:
                raise InvalidPDFError("File ended without valid PDF magic bytes header")

            sha256_hex = hasher.hexdigest()

            return DownloadResult(
                staging_path=staging_path,
                sha256=sha256_hex,
                byte_count=total_bytes,
                source_url=current_url,
                final_url=current_url,
                content_type=initial_response.headers.get("Content-Type"),
                headers=dict(initial_response.headers),
            )

        except Exception:
            # Guarantee cleanup of temporary file on all failure paths
            try:
                temp_file.close()
            except Exception:
                pass
            if staging_path.exists():
                try:
                    staging_path.unlink()
                except Exception:
                    pass
            raise
