import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.url_shortener import create_app
from src.guardrails import PolicyGuardrailEngine


class TestUrlShortenerApp(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "urls.db"
        self.app = create_app(str(self.database_path))

    def tearDown(self):
        self.temp_dir.cleanup()

    def request(self, method, path, payload=None, raw_body=None, host="short.test"):
        if raw_body is not None:
            body = raw_body
        elif payload is not None:
            body = json.dumps(payload).encode("utf-8")
        else:
            body = b""

        captured = {}
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "CONTENT_LENGTH": str(len(body)),
            "CONTENT_TYPE": "application/json",
            "HTTP_HOST": host,
            "wsgi.url_scheme": "https",
            "wsgi.input": io.BytesIO(body),
        }
        response_body = b"".join(self.app(environ, lambda status, headers: captured.update(
            status=status, headers=dict(headers)
        )))
        captured["body"] = response_body
        return captured

    def test_unique_codes_redirect_and_click_analytics(self):
        first = self.request("POST", "/shorten", {"long_url": "https://example.com/one"})
        second = self.request("POST", "/shorten", {"long_url": "https://example.com/two"})

        self.assertTrue(first["status"].startswith("201"))
        self.assertTrue(second["status"].startswith("201"))
        first_code = json.loads(first["body"])["short_code"]
        second_code = json.loads(second["body"])["short_code"]
        self.assertNotEqual(first_code, second_code)

        for code, target, click_count in (
            (first_code, "https://example.com/one", 2),
            (second_code, "https://example.com/two", 1),
        ):
            for _ in range(click_count):
                redirect = self.request("GET", f"/{code}")
                self.assertTrue(redirect["status"].startswith("302"))
                self.assertEqual(redirect["headers"]["Location"], target)

            analytics = self.request("GET", f"/analytics/{code}")
            self.assertTrue(analytics["status"].startswith("200"))
            self.assertEqual(json.loads(analytics["body"])["click_count"], click_count)

    def test_unknown_short_code_returns_404(self):
        response = self.request("GET", "/missing-code")
        self.assertTrue(response["status"].startswith("404"))
        analytics = self.request("GET", "/analytics/missing-code")
        self.assertTrue(analytics["status"].startswith("404"))

    def test_short_url_uses_configured_origin_not_request_host(self):
        response = self.request(
            "POST",
            "/shorten",
            {"long_url": "https://example.com/one"},
            host="attacker.example",
        )

        self.assertTrue(response["status"].startswith("201"))
        short_url = json.loads(response["body"])["short_url"]
        self.assertTrue(short_url.startswith("http://localhost:8000/"))

    def test_public_base_url_must_be_an_origin(self):
        with self.assertRaisesRegex(ValueError, "must be an HTTP or HTTPS origin"):
            create_app(str(Path(self.temp_dir.name) / "invalid.db"), public_base_url="https://short.test/path")

    def test_expiring_links_require_timezone_and_stop_redirecting(self):
        expiration = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        created = self.request("POST", "/shorten", {
            "long_url": "https://example.com/temporary",
            "expires_at": expiration,
        })
        naive_expiration = self.request("POST", "/shorten", {
            "long_url": "https://example.com/naive",
            "expires_at": "2030-01-01T00:00:00",
        })
        self.assertTrue(created["status"].startswith("201"))
        self.assertTrue(naive_expiration["status"].startswith("400"))

        overlong_expiration = self.request("POST", "/shorten", {
            "long_url": "https://example.com/overlong",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(),
        })
        self.assertTrue(overlong_expiration["status"].startswith("400"))

        default_expiration = self.request("POST", "/shorten", {
            "long_url": "https://example.com/default-expiration",
        })
        self.assertTrue(default_expiration["status"].startswith("201"))
        default_code = json.loads(default_expiration["body"])["short_code"]
        with closing(sqlite3.connect(self.database_path)) as connection:
            stored_expiration = connection.execute(
                "SELECT expires_at FROM urls WHERE short_code = ?", (default_code,)
            ).fetchone()[0]
        self.assertIsNotNone(stored_expiration)
        self.assertGreater(
            datetime.fromisoformat(stored_expiration),
            datetime.now(timezone.utc) + timedelta(hours=23),
        )

        short_code = json.loads(created["body"])["short_code"]
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute(
                    "UPDATE urls SET expires_at = ? WHERE short_code = ?",
                    ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), short_code),
                )
        expired = self.request("GET", f"/{short_code}")
        self.assertTrue(expired["status"].startswith("410"))

    def test_expiration_is_rejected_when_capability_is_disabled(self):
        app = create_app(
            str(Path(self.temp_dir.name) / "limited.db"),
            enabled_capabilities=("shorten_urls", "redirect_urls"),
        )
        body = json.dumps({
            "long_url": "https://example.com/temporary",
            "expires_at": "2030-01-01T00:00:00Z",
        }).encode("utf-8")
        response = {}
        app({
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/shorten",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }, lambda status, headers: response.update(status=status))
        self.assertTrue(response["status"].startswith("400"))

    def test_malformed_json_and_invalid_url_are_rejected(self):
        malformed = self.request("POST", "/shorten", raw_body=b"{")
        invalid_url = self.request("POST", "/shorten", {"long_url": "javascript:alert(1)"})
        self.assertTrue(malformed["status"].startswith("400"))
        self.assertTrue(invalid_url["status"].startswith("400"))

    def test_short_code_collision_retries_with_new_code(self):
        with patch("src.url_shortener.secrets.token_urlsafe", side_effect=["same", "same", "unique"]):
            first = self.request("POST", "/shorten", {"long_url": "https://example.com/one"})
            second = self.request("POST", "/shorten", {"long_url": "https://example.com/two"})

        self.assertEqual(json.loads(first["body"])["short_code"], "same")
        self.assertEqual(json.loads(second["body"])["short_code"], "unique")

    def test_custom_alias_is_supported_and_conflicts_are_rejected(self):
        first = self.request(
            "POST", "/shorten",
            {"long_url": "https://example.com/custom", "custom_alias": "my-link_1"},
        )
        duplicate = self.request(
            "POST", "/shorten",
            {"long_url": "https://example.com/other", "custom_alias": "my-link_1"},
        )
        invalid = self.request(
            "POST", "/shorten",
            {"long_url": "https://example.com/invalid", "custom_alias": "bad alias"},
        )

        self.assertTrue(first["status"].startswith("201"))
        self.assertEqual(json.loads(first["body"])["short_code"], "my-link_1")
        self.assertTrue(duplicate["status"].startswith("409"))
        self.assertTrue(invalid["status"].startswith("400"))
        redirect = self.request("GET", "/my-link_1")
        self.assertEqual(redirect["headers"]["Location"], "https://example.com/custom")

    def test_artifact_guardrails_restrict_paths_and_unsafe_python(self):
        with tempfile.TemporaryDirectory() as output_dir:
            permitted = str(Path(output_dir) / "generated.py")
            secret = str(Path(output_dir) / ".env")
            outside = str(Path(output_dir).parent / "outside.py")

            self.assertTrue(PolicyGuardrailEngine.validate_file_operation(permitted, output_dir))
            self.assertFalse(PolicyGuardrailEngine.validate_file_operation(secret, output_dir))
            self.assertFalse(PolicyGuardrailEngine.validate_file_operation(outside, output_dir))
            self.assertFalse(PolicyGuardrailEngine.evaluate_code_safety("value = eval('1 + 1')"))
            self.assertFalse(PolicyGuardrailEngine.evaluate_code_safety("exec('pass')"))
            self.assertTrue(PolicyGuardrailEngine.evaluate_code_safety("def create_app():\n    return object()\n"))


if __name__ == "__main__":
    unittest.main()