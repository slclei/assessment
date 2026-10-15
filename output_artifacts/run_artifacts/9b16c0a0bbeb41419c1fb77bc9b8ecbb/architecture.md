# URL Shortener Architecture

## Components
- WSGI HTTP boundary handles shortening, redirect, and analytics requests.
- SQLite stores short-code mappings and click events.
- The orchestration layer gates schema migration and validates generated artifacts.

## Data Flow
Shorten requests are validated before persistence. Redirect requests resolve a code and record a click in one operation. Analytics reports aggregate counts without visitor identifiers.
