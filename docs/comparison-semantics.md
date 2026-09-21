# Comparison semantics

[← Back to README](../README.md)

Full rules governing how `jsonl-diff` parses records, defines identity,
filters records, compares content, and orders output. See the main
[README](../README.md) for installation and CLI/API usage.

## Strict JSONL records

- Every physical line must be non-blank, valid JSON containing exactly one top-level object.
- Local paths and binary streams are decoded as UTF-8. Text streams are
  already decoded, while URL response charset handling follows `py-jsonl`
  (defaulting to UTF-8 when no charset is declared).
- Blank lines, malformed JSON, duplicate object property names, `NaN`, `Infinity`, and `-Infinity` are errors.
- Top-level arrays, strings, numbers, booleans, and `null` are rejected.
- Input errors identify `OLD` or `NEW` and include the physical line when available.

No normal summary or details output begins until both inputs pass complete
input and duplicate validation.

## Identity and duplicates

Identity fields are top-level object-member names. Each component must exist
and be a string, number, boolean, or (composite identities only) `null`;
objects and arrays are invalid. A single-field identity may not be `null`,
since that would collapse every `null` record into one indistinguishable
identity; a `null` value is only tolerated as one component of a composite
(multi-field) identity. Uniqueness is still enforced on the complete
identity tuple, so a tuple containing only `null` values can still collide.
Nested identity paths and automatic key detection are not supported.

Composite identity field names are sorted lexically before their values are
extracted. Consequently, `key=("b", "a")` and `key=("a", "b")` both produce
keys in `(a, b)` order and match identically. Repeated or empty identity-field
names are configuration errors. On the CLI, `--key` names are trimmed of
surrounding whitespace before this check, so `--key " country , customer "`
is equivalent to `--key country,customer`.

JSON types remain significant: `"1"` is different from `1`, and `true` is
different from `1`.

Every normalized identity must be unique within OLD and within NEW by default.
The first duplicate aborts the comparison; `DuplicateKeyError` reports the
first occurrence and repeated physical line. Set `duplicates="first"` to keep
the first occurrence or `duplicates="last"` to keep the last occurrence and
its physical line. These tolerant policies depend on physical input order.

## Ignored object members

Ignore expressions are exact
[RFC 6901 JSON Pointers](https://www.rfc-editor.org/rfc/rfc6901). Escapes such
as `~1` for `/` and `~0` for `~` are supported. An absent path is harmless,
and `/updated_at` does not match `/metadata/updated_at`.

Ignores are applied symmetrically before content canonicalization. A pointer
may remove an entire array-valued object member, but it may not traverse an
array or address an array element. Ignoring a top-level identity field is also
invalid. Wildcards, JSONPath, and recursive name matching are not supported.

## Filtering with `--where`

`--where` accepts a [JMESPath](https://jmespath.org/) expression, evaluated
against each raw record as it is parsed, before identity extraction, before
`--ignore` removal, and before canonicalization. Only records for which the
expression evaluates to a truthy value participate in the comparison; the
rest are skipped as if they were absent from that source. Skipped records are
never indexed, so they cannot be reported as `added`, `deleted`, or
`modified`, and they never trigger duplicate-key detection.

`--key`, `--where`, and `--ignore` have distinct, non-overlapping
responsibilities: `--key` defines identity, `--where` defines which records
participate at all, and `--ignore` defines which fields are excluded from
content comparison once a record has been selected.

The expression is parsed and validated once, before either input is read; an
invalid expression is a `ConfigurationError` (CLI exit code `3`). The parsed
expression is then reused for every record; it is never recompiled inside the
per-record loop. `--where` is applied independently to OLD and to NEW, so a
record may be filtered out of one side and kept on the other, which is
reported like any other addition or deletion.

JSON literals in a JMESPath expression are written between backticks, so a
string value must be quoted twice: `` `"ES"` ``. The bare form `` `ES` `` also
works but is a deprecated JMESPath syntax that raises a
`PendingDeprecationWarning`.

## Canonical content and numbers

Records with the same identity are compared after ignored members are removed:

- JSON object property order is insignificant at every depth.
- Array order is significant.
- Unicode strings are compared by code-point sequence; no Unicode
  normalization is applied.
- Numbers are parsed as `Decimal` and compared by mathematical value.
  `1`, `1.0`, and `1e0` are equal, and negative zero equals zero.
- Arbitrary-precision integers and decimal values are not rounded through
  binary floating point.

Canonical content is reduced to its length and a SHA-256 digest before being
stored; content is considered equal when both match. A length+SHA-256 match
is treated as proof of equality (the same trade-off relied upon by tools such
as `git` and `rsync`); the full canonical bytes are not retained for
comparison. Numbers are written using compact scientific notation; a large
exponent does not expand into a large string of zeroes.

## Determinism

Summary counts do not depend on either source's physical record order.
Changes, including details records, use the stable lexical byte order of each
canonical typed identity. This is a reproducibility guarantee, not numeric,
locale-aware, or human-oriented sorting.
