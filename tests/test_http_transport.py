from __future__ import annotations

import ssl
import io
import unittest
import urllib.error
import urllib.request
from email.message import Message
from unittest import mock

from silicon_cli.http_transport import (
    Origin,
    PinnedOriginRedirectHandler,
    UnsafeGlassURL,
    glass_endpoint,
    open_pinned,
    validate_glass_server,
)
from silicon_cli import sync


class GlassURLValidationTests(unittest.TestCase):
    def test_requires_https_except_exact_loopback_hosts(self):
        self.assertEqual(
            validate_glass_server("https://glass.example/"),
            "https://glass.example",
        )
        self.assertEqual(
            validate_glass_server("http://127.0.0.1:8000"),
            "http://127.0.0.1:8000",
        )
        self.assertEqual(
            validate_glass_server("http://[::1]:8000"),
            "http://[::1]:8000",
        )
        for value in (
            "http://glass.example",
            "http://localtest.me",
            "ftp://glass.example",
        ):
            with self.subTest(value=value), self.assertRaises(UnsafeGlassURL):
                validate_glass_server(value)

    def test_rejects_ambiguous_or_secret_bearing_base_urls(self):
        for value in (
            "https://user:pass@glass.example",
            "https://glass.example/path",
            "https://glass.example?token=secret",
            "https://glass.example/#fragment",
            "https://glass.example\\@attacker.example",
        ):
            with self.subTest(value=value), self.assertRaises(UnsafeGlassURL):
                validate_glass_server(value)

    def test_endpoint_builder_rejects_authority_query_and_fragment(self):
        for path in (
            "//attacker.example/collect",
            "/api/v1/pull?secret=value",
            "/api/v1/pull#fragment",
            "/api\\v1\\pull",
        ):
            with self.subTest(path=path), self.assertRaises(UnsafeGlassURL):
                glass_endpoint("https://glass.example", path)


class PinnedRedirectTests(unittest.TestCase):
    def setUp(self):
        self.request = urllib.request.Request(
            "https://glass.example/api/v1/teams/setup-pull",
            data=b'{"pull_transaction_id":"abc"}',
            headers={"X-Team-Key": "never-forward-me"},
            method="POST",
        )
        self.headers = Message()

    def test_cross_origin_redirect_is_rejected_before_request_is_built(self):
        handler = PinnedOriginRedirectHandler(
            Origin("https", "glass.example", 443)
        )
        with (
            mock.patch.object(
                urllib.request.HTTPRedirectHandler,
                "redirect_request",
            ) as parent,
            self.assertRaises(urllib.error.HTTPError),
        ):
            handler.redirect_request(
                self.request,
                None,
                307,
                "redirect",
                self.headers,
                "https://attacker.example/collect",
            )
        parent.assert_not_called()

    def test_https_to_http_downgrade_is_rejected(self):
        handler = PinnedOriginRedirectHandler(
            Origin("https", "glass.example", 443)
        )
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(
                self.request,
                None,
                307,
                "redirect",
                self.headers,
                "http://glass.example/collect",
            )

    def test_final_response_url_is_checked_even_without_redirect_callback(self):
        response = mock.Mock()
        response.geturl.return_value = "https://attacker.example/collect"
        opener = mock.Mock()
        opener.open.return_value = response
        with (
            mock.patch.object(urllib.request, "build_opener", return_value=opener),
            self.assertRaises(UnsafeGlassURL),
        ):
            open_pinned(
                self.request,
                timeout=30,
                context=ssl.create_default_context(),
            )
        response.close.assert_called_once()


class BoundedJSONResponseTests(unittest.TestCase):
    class Response:
        def __init__(self, body: bytes, *, status: int = 200, length=None):
            self.body = io.BytesIO(body)
            self.status = status
            self.headers = Message()
            if length is not None:
                self.headers["Content-Length"] = str(length)

        def read(self, size=-1):
            return self.body.read(size)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def test_oversized_success_body_is_bounded_and_rejected(self):
        response = self.Response(
            b"x" * (sync.MAX_GLASS_JSON_RESPONSE_BYTES + 1)
        )
        with mock.patch.object(sync, "_urlopen", return_value=response):
            status, body = sync._post_json("https://glass.example/endpoint")

        self.assertEqual(status, 0)
        self.assertIn("too large", body["detail"])
        self.assertEqual(
            response.body.tell(), sync.MAX_GLASS_JSON_RESPONSE_BYTES + 1
        )

    def test_oversized_http_error_body_is_bounded_and_discarded(self):
        error_body = io.BytesIO(
            b"x" * (sync.MAX_GLASS_JSON_RESPONSE_BYTES + 1)
        )
        headers = Message()
        error = urllib.error.HTTPError(
            "https://glass.example/endpoint",
            413,
            "too large",
            headers,
            error_body,
        )
        with mock.patch.object(sync, "_urlopen", side_effect=error):
            status, body = sync._post_json("https://glass.example/endpoint")

        self.assertEqual((status, body), (413, {}))
        self.assertEqual(
            error_body.tell(), sync.MAX_GLASS_JSON_RESPONSE_BYTES + 1
        )

    def test_success_json_must_be_an_object(self):
        response = self.Response(b"[]")
        with mock.patch.object(sync, "_urlopen", return_value=response):
            status, body = sync._post_json("https://glass.example/endpoint")

        self.assertEqual(status, 0)
        self.assertIn("must be an object", body["detail"])


if __name__ == "__main__":
    unittest.main()
