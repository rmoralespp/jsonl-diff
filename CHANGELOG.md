## Releases

## Unreleased

- **Added:** `--field-diff` enriches modified `--details` events with deterministic RFC 6901 field changes using a disk-backed second pass over rereadable sources.
- **Added:** `--schema-diff` and `schema_diff=True` compare disk-backed observed
  field profiles, reporting added/removed fields and changes in types,
  nullability, and requiredness. `--schema-ignore` provides independent RFC
  6901 exclusions for schema profiling.
- **Added:** Tolerated duplicate identities are counted per source and exposed
  as deterministic API and `--details` diagnostics with selected/discarded
  physical lines and canonical-content equality.
- **Changed:** Tolerated duplicates now produce CLI exit code `1`, even when
  the selected OLD and NEW records otherwise compare equal.
- **Docs:** Clarify that `max_temp` is a best-effort budget for the `jsonl-diff` workspace, not a global limit for all temporary storage used by the process.
- **Docs:** Clarify that composite identities may contain `null` components, but uniqueness is always enforced on the complete identity tuple.
- **Changed:** Internal JSON value types now distinguish parser-produced `Decimal` numbers from native Python `int`/`float` values accepted only by numeric canonicalization helpers.
- **Changed:** Compile `--where` expressions once and reuse them during indexing.
- **Changed:** Run `--max-temp` size checks periodically, improving large-input indexing speed by ~8x while retaining limits.
- **Fixed:** Trim whitespace from `--key` names and reject empty values correctly.
- **Fixed:** Links to documentation files referenced from the README file
- **Fixed:** Details JSONL writes ordinary counters, line numbers, keys, and field values without forcing scientific notation.

## v0.1.1

- **Added:** `--where` to filter which records participate in the comparison using a JMESPath expression.
- **Changed:** The tmp SQLite index no longer stores the full canonical content BLOB.
- **Changed:** A `null` value is now accepted for a component of a composite (multi-field) `--key`.
- **Changed:** README synthesized into a quick-start-focused overview; detailed reference material moved to `docs/`.


## v0.1.0

- **Added:** initial commit - MVP
- **Added:** Readme badges
