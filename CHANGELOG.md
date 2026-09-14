## Releases

## Unreleased

- **Added:** `--where EXPRESSION` (and the `where=` keyword in the Python
  `diff()` API) to filter which records participate in the comparison using a
  [JMESPath](https://jmespath.org/) expression, evaluated once per record
  before identity extraction and before `--ignore` removal. Records for which
  the expression is not truthy are skipped on that side entirely: they are
  never indexed, never reported as added/deleted/modified, and never trigger
  duplicate-key detection. The expression is parsed and validated once,
  before either input is read, and the parsed expression is reused for every
  record. Adds a mandatory runtime dependency on `jmespath`. (#1)
- **Changed:** the tmp SQLite index no longer stores the full canonical
  content BLOB; content equality is now based on length + SHA-256 digest
  instead of a byte-for-byte canonical comparison.
- **Changed:** a `null` value is now accepted for a component of a composite
  (multi-field) `--key`, as long as the other components keep the identity
  selective. A single-field `--key` still rejects `null`.

## v0.1.0

- **Added:** initial commit - MVP
- **Added:** Readme badges
