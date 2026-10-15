# Setup and Operations

Scope: Build a resilient URL shortener with redirects, click analytics, and custom aliases.

## Assumptions
- Collect aggregate counts only until a retention policy is approved.
- Require globally unique aliases for the initial design.
- Validate local failure controls only; do not claim high availability until an SLO and deployment design are approved.

## Open Questions
- What retention period should apply to analytics data?
- Are aliases globally unique or scoped to individual users?
- What availability target and recovery objective should the service meet?
