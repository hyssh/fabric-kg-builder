# Changelog

## 0.2.4

- Added strict `app deploy-l7` planning with dry-run as the default, immutable
  plan hashing, exact live approval, expiry/drift gates, and sanitized rollback
  receipts.
- Added exact Foundry project-connection bearer authentication and readback.
- Added Python 3.12 external installed-CLI acceptance tooling and release proof
  templates.
- Kept the 0.2.4 acceptance path local and CLI-only.
- Hardened long-running schema-2 enrichment: transient Foundry transport
  failures (connectivity, timeout, throttling, server faults) are retried with
  bounded backoff instead of aborting a resumable run, while request,
  authentication, authorization, and validation failures still fail fast.
  A single empty or unparseable model completion is likewise retried and, if
  it persists, reported as an exact empty-completion failure rather than an
  opaque JSON parse position.
