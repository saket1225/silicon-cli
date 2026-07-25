"""Fail-closed HTTP transport for credential-bearing Glass requests."""
from __future__ import annotations

import ipaddress
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


class UnsafeGlassURL(ValueError):
    pass


@dataclass(frozen=True)
class Origin:
    scheme: str
    host: str
    port: int

    @property
    def url(self) -> str:
        default = 443 if self.scheme == "https" else 80
        host = f"[{self.host}]" if ":" in self.host else self.host
        suffix = "" if self.port == default else f":{self.port}"
        return f"{self.scheme}://{host}{suffix}"


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def origin_from_url(url: str, *, require_safe_scheme: bool = True) -> Origin:
    if (
        not isinstance(url, str)
        or not url
        or any(ord(character) < 0x20 for character in url)
        or "\\" in url
    ):
        raise UnsafeGlassURL("Glass URL is malformed.")
    parsed = urllib.parse.urlsplit(url)
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeGlassURL("Glass URL must not contain user information.")
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or parsed.netloc != parsed.netloc.strip():
        raise UnsafeGlassURL("Glass URL must contain one canonical host.")
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeGlassURL("Glass URL has an invalid port.") from exc
    if not 1 <= port <= 65535:
        raise UnsafeGlassURL("Glass URL has an invalid port.")
    if require_safe_scheme and not (
        scheme == "https" or (scheme == "http" and _is_loopback(host))
    ):
        raise UnsafeGlassURL(
            "Glass must use HTTPS; HTTP is allowed only for a loopback host."
        )
    if scheme not in {"https", "http"}:
        raise UnsafeGlassURL("Glass URL has an unsupported scheme.")
    return Origin(scheme, host, port)


def validate_glass_server(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or ""))
    origin = origin_from_url(url)
    if parsed.query or parsed.fragment:
        raise UnsafeGlassURL("Glass server URL must not contain a query or fragment.")
    if parsed.path not in {"", "/"}:
        raise UnsafeGlassURL("Glass server URL must not contain a path.")
    return origin.url


def glass_endpoint(server: str, path: str) -> str:
    base = validate_glass_server(server)
    if not isinstance(path, str):
        raise UnsafeGlassURL("Glass endpoint path is malformed.")
    parsed = urllib.parse.urlsplit(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise UnsafeGlassURL("Glass endpoint path is malformed.")
    return base + path


def assert_same_origin(url: str, expected: Origin) -> None:
    actual = origin_from_url(url)
    if actual != expected:
        raise UnsafeGlassURL(
            "Glass redirected a credential-bearing request to another origin."
        )


class PinnedOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Permit redirects only when scheme, host, and effective port are exact."""

    def __init__(self, expected: Origin):
        super().__init__()
        self.expected = expected

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        resolved = urllib.parse.urljoin(req.full_url, newurl)
        try:
            assert_same_origin(resolved, self.expected)
        except UnsafeGlassURL as exc:
            # Raise before urllib creates a new Request and copies headers/body.
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                str(exc),
                headers,
                fp,
            ) from exc
        return super().redirect_request(req, fp, code, msg, headers, resolved)


def open_pinned(
    request: urllib.request.Request,
    *,
    timeout: int | float | None,
    context: ssl.SSLContext,
):
    expected = origin_from_url(request.full_url)
    opener = urllib.request.build_opener(
        PinnedOriginRedirectHandler(expected),
        urllib.request.HTTPSHandler(context=context),
    )
    response = opener.open(request, timeout=timeout)
    try:
        assert_same_origin(response.geturl(), expected)
    except Exception:
        response.close()
        raise
    return response
