import importlib.util
import io
import json
import socket
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


def _deny_network(*args, **kwargs):
    raise PermissionError("Network access is disabled during acceptance tests.")


socket.socket = _deny_network
socket.create_connection = _deny_network


def _call_wsgi(application, method, path, payload=None, raw_body=None):
    if raw_body is not None:
        body = raw_body
    else:
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    response = {}
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }
    response_body = b"".join(application(environ, lambda status, headers: response.update(
        status=status, headers=dict(headers)
    )))
    response["body"] = response_body
    return response


def run(app_path, requirement_ids, default_expiration_hours=24, max_expiration_hours=None):
    if max_expiration_hours is None:
        max_expiration_hours = default_expiration_hours
    module_spec = importlib.util.spec_from_file_location("planned_url_shortener", app_path)
    if module_spec is None or module_spec.loader is None:
        raise ValueError("Could not load generated application entry point.")
    generated_app = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(generated_app)

    checks = []
    with tempfile.TemporaryDirectory() as acceptance_dir:
        database = Path(acceptance_dir) / "acceptance.db"
        application = generated_app.create_app(str(database))
        first = _call_wsgi(application, "POST", "/shorten", {"long_url": "https://example.com/one"})
        second = _call_wsgi(application, "POST", "/shorten", {"long_url": "https://example.com/two"})
        if not first["status"].startswith("201") or not second["status"].startswith("201"):
            raise AssertionError("Shorten endpoint did not create both URLs.")
        first_code = json.loads(first["body"])["short_code"]
        second_code = json.loads(second["body"])["short_code"]
        if first_code == second_code:
            raise AssertionError("Short codes were not unique.")
        checks.append("two URLs receive distinct short codes")

        for short_code, expected_url in (
            (first_code, "https://example.com/one"),
            (second_code, "https://example.com/two"),
        ):
            redirect = _call_wsgi(application, "GET", f"/{short_code}")
            if not redirect["status"].startswith("302") or redirect["headers"].get("Location") != expected_url:
                raise AssertionError(f"Redirect check failed for {short_code}.")
        checks.append("both short codes redirect to their destinations")

        if "custom_aliases" in requirement_ids:
            alias = _call_wsgi(application, "POST", "/shorten", {
                "long_url": "https://example.com/alias",
                "custom_alias": "local-agent-alias",
            })
            conflict = _call_wsgi(application, "POST", "/shorten", {
                "long_url": "https://example.com/conflict",
                "custom_alias": "local-agent-alias",
            })
            if not alias["status"].startswith("201") or not conflict["status"].startswith("409"):
                raise AssertionError("Custom alias or uniqueness check failed.")
            checks.append("custom aliases are accepted and collisions rejected")

        if "reliability_controls" in requirement_ids:
            with closing(sqlite3.connect(database)) as connection:
                with connection:
                    connection.execute(
                        "INSERT INTO urls (short_code, long_url) VALUES (?, ?)",
                        ("collision-code", "https://example.com/existing"),
                    )
            with patch.object(generated_app.secrets, "token_urlsafe", side_effect=["collision-code", "retry-code"]):
                collision_retry = _call_wsgi(application, "POST", "/shorten", {
                    "long_url": "https://example.com/retry",
                })
            oversized = _call_wsgi(application, "POST", "/shorten", {
                "long_url": "https://example.com/" + "a" * 1_048_600,
            })
            if (
                not collision_retry["status"].startswith("201")
                or json.loads(collision_retry["body"]).get("short_code") != "retry-code"
                or not oversized["status"].startswith("413")
            ):
                raise AssertionError("Bounded collision retry or request-size protection failed.")
            checks.append("collision retries recover and oversized requests are rejected")

        if "expiring_links" in requirement_ids:
            expiring = _call_wsgi(application, "POST", "/shorten", {
                "long_url": "https://example.com/expiring",
            })
            invalid_expiration = _call_wsgi(application, "POST", "/shorten", {
                "long_url": "https://example.com/invalid-expiration",
                "expires_at": "2030-01-01T00:00:00",
            })
            overlong_expiration = _call_wsgi(application, "POST", "/shorten", {
                "long_url": "https://example.com/overlong-expiration",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=max_expiration_hours + 1)).isoformat(),
            })
            if not expiring["status"].startswith("201") or not invalid_expiration["status"].startswith("400"):
                raise AssertionError("Expiration creation or timezone validation failed.")
            if not overlong_expiration["status"].startswith("400"):
                raise AssertionError("Expiration longer than 24 hours was accepted.")
            expiring_code = json.loads(expiring["body"])["short_code"]
            with closing(sqlite3.connect(database)) as connection:
                expires_at = connection.execute(
                    "SELECT expires_at FROM urls WHERE short_code = ?", (expiring_code,)
                ).fetchone()[0]
                if expires_at is None:
                    raise AssertionError("New expiring links did not receive the default expiration.")
                remaining_hours = (
                    datetime.fromisoformat(expires_at) - datetime.now(timezone.utc)
                ).total_seconds() / 3600
                if not default_expiration_hours - 0.01 <= remaining_hours <= default_expiration_hours:
                    raise AssertionError("New links did not receive the requested default expiration.")
                with connection:
                    connection.execute(
                        "UPDATE urls SET expires_at = ? WHERE short_code = ?",
                        ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), expiring_code),
                    )
            expired_redirect = _call_wsgi(application, "GET", f"/{expiring_code}")
            if not expired_redirect["status"].startswith("410"):
                raise AssertionError("Expired links were not rejected with HTTP 410.")
            expiration_check = (
                f"new links expire by default after {default_expiration_hours} hours; "
                "overrides are bounded and expired redirects return 410"
            )
            checks.append(expiration_check)

        malformed = _call_wsgi(application, "POST", "/shorten", raw_body=b"{")
        invalid_url = _call_wsgi(application, "POST", "/shorten", {"long_url": "javascript:alert(1)"})
        missing = _call_wsgi(application, "GET", "/not-a-code")
        if not malformed["status"].startswith("400") or not invalid_url["status"].startswith("400"):
            raise AssertionError("Malformed or unsafe inputs were not rejected.")
        if not missing["status"].startswith("404"):
            raise AssertionError("Unknown code did not return 404.")
        checks.extend(["malformed input rejected", "unsafe URL rejected", "unknown code returns 404"])
        if "click_analytics" in requirement_ids:
            analytics = _call_wsgi(application, "GET", f"/analytics/{first_code}")
            if not analytics["status"].startswith("200") or json.loads(analytics["body"])["click_count"] != 1:
                raise AssertionError("Click analytics did not record the redirect.")
            checks.append("click analytics records redirects")

    required_checks = {
        "shorten_urls": "two URLs receive distinct short codes",
        "redirect_urls": "both short codes redirect to their destinations",
        "click_analytics": "click analytics records redirects",
        "custom_aliases": "custom aliases are accepted and collisions rejected",
        "reliability_controls": "collision retries recover and oversized requests are rejected",
        "expiring_links": expiration_check if "expiring_links" in requirement_ids else "",
    }
    validated_requirement_ids = sorted(
        requirement_id
        for requirement_id, check in required_checks.items()
        if requirement_id in requirement_ids and check in checks
    )
    if set(validated_requirement_ids) != set(requirement_ids):
        raise AssertionError("Acceptance checks did not cover every planned requirement.")
    return {
        "passed": True,
        "checks": checks,
        "validated_requirement_ids": validated_requirement_ids,
    }
if __name__ == "__main__":
    default_expiration_hours = int(sys.argv[4]) if len(sys.argv) > 4 else 24
    max_expiration_hours = int(sys.argv[5]) if len(sys.argv) > 5 else default_expiration_hours
    report = run(
        sys.argv[1], json.loads(sys.argv[2]),
        default_expiration_hours, max_expiration_hours,
    )
    Path(sys.argv[3]).write_text(json.dumps(report, indent=2), encoding="utf-8")