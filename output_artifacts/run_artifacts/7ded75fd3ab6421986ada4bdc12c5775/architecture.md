# Architecture Design

Request: Build a resilient URL shortener with redirects, click analytics, and custom aliases.

## Requirements
- shorten_urls: Create unique short codes for valid URLs.
- redirect_urls: Resolve known short codes to their destination URLs.
- click_analytics: Record and report aggregate link usage.
- custom_aliases: Support user-selected short codes subject to collision checks.
- reliability_controls: Use bounded short-code collision retries and request-size limits; no high-availability guarantee is claimed.

## Components
- URL/API boundary
- Persistence adapter
- Policy and validation gates
