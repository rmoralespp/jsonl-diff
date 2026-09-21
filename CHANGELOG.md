## Releases

## Unreleased

- **Fixed:** CLI `--key` names are now trimmed of surrounding whitespace before validation, so comma/repeat-separated values like `--key " country , customer "` no longer produce a spurious "missing identity field" error and empty/whitespace-only names are correctly rejected as configuration errors.

## v0.1.1

- **Added:** `--where` to filter which records participate in the comparison using a JMESPath expression.
- **Changed:** The tmp SQLite index no longer stores the full canonical content BLOB.
- **Changed:** A `null` value is now accepted for a component of a composite (multi-field) `--key`.
- **Changed:** README synthesized into a quick-start-focused overview; detailed reference material moved to `docs/`.


## v0.1.0

- **Added:** initial commit - MVP
- **Added:** Readme badges
