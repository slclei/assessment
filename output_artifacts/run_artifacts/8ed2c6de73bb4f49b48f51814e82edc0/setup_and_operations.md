# URL Shortener Setup and Operations

Run from the project directory with `python output_artifacts/main.py` after approving the SQLite migration.

## Endpoints
- `POST /shorten` creates a short code.
- `GET /{short_code}` redirects and records a click.
- `GET /analytics/{short_code}` returns aggregate click counts.

## Operational Notes
Data is stored in `urls.db`. Back up the database before schema changes. The prototype does not collect visitor identifiers.
