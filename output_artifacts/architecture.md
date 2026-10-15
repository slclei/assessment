# Architecture Design

Request: Set the default lifetime to 7 days for new links in my URL shortener.

## Requirements
- shorten_urls: Create unique short codes for valid URLs.
- expiring_links: Apply the normalized default link lifetime, allow shorter timezone-aware expirations, and reject redirects after expiry.
- redirect_urls: Resolve known short codes to their destination URLs.

## Components
- URL/API boundary
- Persistence adapter
- Policy and validation gates
