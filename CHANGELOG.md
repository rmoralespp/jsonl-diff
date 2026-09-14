## Releases

## Unreleased

- **Changed:** the tmp SQLite index no longer stores the full canonical
  content BLOB; content equality is now based on length + SHA-256 digest
  instead of a byte-for-byte canonical comparison.
- **Changed:** a `null` value is now accepted for a component of a composite
  (multi-field) `--key`, as long as the other components keep the identity
  selective. A single-field `--key` still rejects `null`.

## v0.1.0

- **Added:** initial commit - MVP
- **Added:** Readme badges
