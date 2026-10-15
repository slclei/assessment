import json
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from urllib.parse import urlsplit
from wsgiref.simple_server import make_server
SCHEMA_SQL = 'CREATE TABLE IF NOT EXISTS urls (\n    id INTEGER PRIMARY KEY AUTOINCREMENT,\n    short_code TEXT UNIQUE NOT NULL,\n    long_url TEXT NOT NULL,\n    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n    expires_at TEXT\n);\nCREATE TABLE IF NOT EXISTS click_events (\n    id INTEGER PRIMARY KEY AUTOINCREMENT,\n    short_code TEXT NOT NULL REFERENCES urls(short_code) ON DELETE CASCADE,\n    clicked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n);\n'

def _is_valid_url(value):
    if not isinstance(value, str) or not value or value != value.strip() or (len(value) > 2048):
        return False
    if any((ord(character) < 32 for character in value)):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme.lower() in {'http', 'https'} and bool(parsed.hostname) and (parsed.username is None) and (parsed.password is None) and (parsed.port is None or 1 <= parsed.port <= 65535)
    except ValueError:
        return False

def _json_response(start_response, status, payload):
    body = json.dumps(payload).encode('utf-8')
    start_response(status, [('Content-Type', 'application/json; charset=utf-8'), ('Content-Length', str(len(body)))])
    return [body]

def _normalize_expiration(value, max_lifetime_hours):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('expires_at must be an ISO-8601 timestamp with a timezone')
    try:
        expiration = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError('expires_at must be an ISO-8601 timestamp with a timezone') from error
    if expiration.tzinfo is None or expiration.utcoffset() is None:
        raise ValueError('expires_at must include a timezone')
    expiration = expiration.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if expiration <= now:
        raise ValueError('expires_at must be in the future')
    if expiration > now + timedelta(hours=max_lifetime_hours):
        raise ValueError(f'expires_at cannot be more than {max_lifetime_hours} hours in the future')
    return expiration.isoformat()

def _is_expired(value):
    if value is None:
        return False
    try:
        expiration = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return True
    if expiration.tzinfo is None or expiration.utcoffset() is None:
        return True
    return expiration <= datetime.now(timezone.utc)

def create_app(database_path='urls.db', enabled_capabilities=('shorten_urls', 'expiring_links', 'redirect_urls'), public_base_url='http://localhost:8000', default_expiration_hours=168, max_expiration_hours=168):
    if not isinstance(default_expiration_hours, int) or isinstance(default_expiration_hours, bool) or (not isinstance(max_expiration_hours, int)) or isinstance(max_expiration_hours, bool) or (not 1 <= default_expiration_hours <= max_expiration_hours <= 8760):
        raise ValueError('Expiration hours must be integers from 1 to 8760, with the default not exceeding the maximum.')
    parsed_base_url = urlsplit(public_base_url)
    if not _is_valid_url(public_base_url) or parsed_base_url.path not in ('', '/') or parsed_base_url.query or parsed_base_url.fragment:
        raise ValueError('public_base_url must be an HTTP or HTTPS origin without a path, query, or fragment')
    public_base_url = f'{parsed_base_url.scheme}://{parsed_base_url.netloc}'
    supported_capabilities = {'shorten_urls', 'redirect_urls', 'click_analytics', 'custom_aliases', 'reliability_controls', 'expiring_links'}
    capabilities = supported_capabilities if enabled_capabilities is None else set(enabled_capabilities)
    unsupported_capabilities = capabilities - supported_capabilities
    if unsupported_capabilities:
        raise ValueError(f"Unsupported service capabilities: {', '.join(sorted(unsupported_capabilities))}")

    def connect():
        connection = sqlite3.connect(database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        return connection
    with closing(connect()) as connection:
        with connection:
            connection.executescript(SCHEMA_SQL)
            columns = {row[1] for row in connection.execute('PRAGMA table_info(urls)')}
            if 'expires_at' not in columns:
                connection.execute('ALTER TABLE urls ADD COLUMN expires_at TEXT')

    def application(environ, start_response):
        method = environ.get('REQUEST_METHOD', 'GET').upper()
        path = environ.get('PATH_INFO', '')
        if method == 'POST' and path == '/shorten':
            if 'shorten_urls' not in capabilities:
                return _json_response(start_response, '404 Not Found', {'error': 'Not found'})
            try:
                body_size = int(environ.get('CONTENT_LENGTH') or '0')
            except (TypeError, ValueError):
                return _json_response(start_response, '400 Bad Request', {'error': 'Invalid Content-Length'})
            if body_size < 0:
                return _json_response(start_response, '400 Bad Request', {'error': 'Invalid Content-Length'})
            if body_size > 1048576:
                return _json_response(start_response, '413 Payload Too Large', {'error': 'Request body is too large'})
            try:
                body = environ['wsgi.input'].read(body_size)
                data = json.loads(body.decode('utf-8'))
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError):
                return _json_response(start_response, '400 Bad Request', {'error': 'Request body must be valid JSON'})
            if not isinstance(data, dict) or not _is_valid_url(data.get('long_url')):
                return _json_response(start_response, '400 Bad Request', {'error': 'long_url must be a valid HTTP or HTTPS URL'})
            expires_at = data.get('expires_at')
            expiration_enabled = 'expiring_links' in capabilities
            if expires_at is not None and (not expiration_enabled):
                return _json_response(start_response, '400 Bad Request', {'error': 'Link expiration is not enabled'})
            if expiration_enabled:
                if expires_at is None:
                    expires_at = (datetime.now(timezone.utc) + timedelta(hours=default_expiration_hours)).isoformat()
                else:
                    try:
                        expires_at = _normalize_expiration(expires_at, max_expiration_hours)
                    except ValueError as error:
                        return _json_response(start_response, '400 Bad Request', {'error': str(error)})
            custom_alias = data.get('custom_alias')
            if custom_alias is not None:
                if 'custom_aliases' not in capabilities:
                    return _json_response(start_response, '400 Bad Request', {'error': 'Custom aliases are not enabled'})
                if not isinstance(custom_alias, str) or not re.fullmatch('[A-Za-z0-9_-]{1,32}', custom_alias):
                    return _json_response(start_response, '400 Bad Request', {'error': 'custom_alias must be 1-32 letters, digits, underscores, or hyphens'})
                try:
                    with closing(connect()) as connection:
                        with connection:
                            connection.execute('INSERT INTO urls (short_code, long_url, expires_at) VALUES (?, ?, ?)', (custom_alias, data['long_url'], expires_at))
                    short_code = custom_alias
                except sqlite3.IntegrityError:
                    return _json_response(start_response, '409 Conflict', {'error': 'custom_alias is already in use'})
            else:
                short_code = None
                for _ in range(5):
                    candidate = secrets.token_urlsafe(6)
                    try:
                        with closing(connect()) as connection:
                            with connection:
                                connection.execute('INSERT INTO urls (short_code, long_url, expires_at) VALUES (?, ?, ?)', (candidate, data['long_url'], expires_at))
                        short_code = candidate
                        break
                    except sqlite3.IntegrityError:
                        continue
            if short_code is None:
                return _json_response(start_response, '503 Service Unavailable', {'error': 'Could not allocate a unique short code'})
            return _json_response(start_response, '201 Created', {'short_code': short_code, 'short_url': f'{public_base_url}/{short_code}'})
        if method == 'GET' and path.startswith('/analytics/'):
            if 'click_analytics' not in capabilities:
                return _json_response(start_response, '404 Not Found', {'error': 'Not found'})
            short_code = path.removeprefix('/analytics/')
            if not re.fullmatch('[A-Za-z0-9_-]{1,64}', short_code):
                return _json_response(start_response, '404 Not Found', {'error': 'Short URL not found'})
            with closing(connect()) as connection:
                with connection:
                    exists = connection.execute('SELECT 1 FROM urls WHERE short_code = ?', (short_code,)).fetchone()
                    if exists is None:
                        return _json_response(start_response, '404 Not Found', {'error': 'Short URL not found'})
                    analytics = connection.execute('SELECT COUNT(*) AS click_count, MAX(clicked_at) AS last_clicked_at FROM click_events WHERE short_code = ?', (short_code,)).fetchone()
            return _json_response(start_response, '200 OK', {'short_code': short_code, 'click_count': analytics['click_count'], 'last_clicked_at': analytics['last_clicked_at']})
        if method == 'GET' and path.startswith('/'):
            if 'redirect_urls' not in capabilities:
                return _json_response(start_response, '404 Not Found', {'error': 'Not found'})
            short_code = path[1:]
            if not re.fullmatch('[A-Za-z0-9_-]{1,64}', short_code):
                return _json_response(start_response, '404 Not Found', {'error': 'Short URL not found'})
            with closing(connect()) as connection:
                with connection:
                    row = connection.execute('SELECT long_url, expires_at FROM urls WHERE short_code = ?', (short_code,)).fetchone()
                    if row is None:
                        return _json_response(start_response, '404 Not Found', {'error': 'Short URL not found'})
                    if _is_expired(row['expires_at']):
                        return _json_response(start_response, '410 Gone', {'error': 'Short URL has expired'})
                    if 'click_analytics' in capabilities:
                        connection.execute('INSERT INTO click_events (short_code) VALUES (?)', (short_code,))
                    long_url = row['long_url']
            start_response('302 Found', [('Location', long_url), ('Content-Length', '0')])
            return [b'']
        return _json_response(start_response, '404 Not Found', {'error': 'Not found'})
    return application

@lru_cache(maxsize=1)
def _default_app():
    return create_app()

def app(environ, start_response):
    return _default_app()(environ, start_response)
if __name__ == '__main__':
    make_server('localhost', 8000, app).serve_forever()
