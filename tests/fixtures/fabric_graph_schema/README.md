# Public Fabric Graph schema fixtures

These are public JSON Schema snapshots for offline compiler regression tests,
not customer data, extraction results or live deployment definitions.

`snapshot.json` records the source URLs, capture date and SHA-256 digest of each
download. Preserve the original bytes, including line endings; `.gitattributes`
disables text conversion for these snapshots. The accompanying Microsoft MIT
license is retained in `LICENSE`.

The manifest also records an unavailable referenced schema. Tests must not
represent this partial snapshot as a complete validation of every external
reference. Refreshing a snapshot requires reviewing its changes and updating its
manifest hashes together.
