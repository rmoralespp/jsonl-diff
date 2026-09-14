## Releases

## Unreleased

- **Changed:** the tmp SQLite index no longer stores the full canonical
  content BLOB; content equality is now based on length + SHA-256 digest
  instead of a byte-for-byte canonical comparison.

## v0.1.0

- **Added:** initial commit - MVP
- **Added:** Readme badges
