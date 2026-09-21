## Releases

## Unreleased

- **Docs:** Clarify that `max_temp` is a best-effort budget for the `jsonl-diff` workspace, not a global limit for all temporary storage used by the process.
- **Docs:** Clarify that composite identities may contain `null` components, but uniqueness is always enforced on the complete identity tuple.
- **Changed:** Internal JSON value types now distinguish parser-produced `Decimal` numbers from native Python `int`/`float` values accepted only by numeric canonicalization helpers.
- **Changed:** Compile `--where` expressions once and reuse them during indexing.
- **Changed:** Run `--max-temp` size checks periodically, improving large-input indexing speed by ~8x while retaining limits.
- **Fixed:** Trim whitespace from `--key` names and reject empty values correctly.
- **Fixed:** Links to documentation files referenced from the README file

## v0.1.1

- **Added:** `--where` to filter which records participate in the comparison using a JMESPath expression.
- **Changed:** The tmp SQLite index no longer stores the full canonical content BLOB.
- **Changed:** A `null` value is now accepted for a component of a composite (multi-field) `--key`.
- **Changed:** README synthesized into a quick-start-focused overview; detailed reference material moved to `docs/`.


## v0.1.0

- **Added:** initial commit - MVP
- **Added:** Readme badges
