## Releases

## Unreleased

- **Changed:** `--where` expressions are now compiled once during configuration validation and reused while indexing both inputs.
- **Changed:** With `--max-temp` set, the temporary-storage size check now runs every 1024 records (plus once after each side finishes) instead of after every single record, cutting indexing time by ~8x on large inputs (200k records: ~58s → ~7s in local testing) while still bounding usage — SQLite's own page-count limit continues to reject oversized writes immediately.
- **Fixed:** CLI `--key` names are now trimmed of surrounding whitespace before validation, so comma/repeat-separated values like `--key " country , customer "` no longer produce a spurious "missing identity field" error and empty/whitespace-only names are correctly rejected as configuration errors.

## v0.1.1

- **Added:** `--where` to filter which records participate in the comparison using a JMESPath expression.
- **Changed:** The tmp SQLite index no longer stores the full canonical content BLOB.
- **Changed:** A `null` value is now accepted for a component of a composite (multi-field) `--key`.
- **Changed:** README synthesized into a quick-start-focused overview; detailed reference material moved to `docs/`.


## v0.1.0

- **Added:** initial commit - MVP
- **Added:** Readme badges
