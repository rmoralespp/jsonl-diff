# jsonl-diff

----

<p>
  <img src="https://img.shields.io/pypi/v/jsonl-diff.svg" alt="PyPI">
  <img src="https://img.shields.io/pypi/pyversions/jsonl-diff.svg" alt="Python">
  <img src="https://github.com/rmoralespp/jsonl-diff/workflows/CI/badge.svg" alt="CI">
  <img src="https://codecov.io/gh/rmoralespp/jsonl-diff/branch/main/graph/badge.svg" alt="Coverage">
  <img src="https://img.shields.io/github/license/rmoralespp/jsonl-diff.svg" alt="License">
</p>

jsonl-diff compares large JSONL/NDJSON datasets by record identity, without loading the entire files 
into memory. It matches records using one or more top-level fields and classifies each record as equal, added, deleted, or modified.

## Why jsonl-diff?

A text diff compares lines. That is usually the wrong model for datasets:
reordering unchanged records creates noise, and inserting one record can make
every later line appear different. `jsonl-diff` fills the gap between
line-oriented tools and in-memory dataframe comparisons by matching records
on identity instead of physical position.

Use it for snapshots, ETL validation, migrations, exports, and CI checks. Use
`diff`, `jq`, or a visual JSON diff instead when you need line-level,
field-level, or JSON Patch output.

Comparisons are disk-backed (see [Disk-backed architecture and temporary
files](#disk-backed-architecture-and-temporary-files)), so complete inputs and
result sets do not need to fit in memory.

## Features

- Single or composite top-level identities, independent of input order.
- Optional `--where` JMESPath filtering to select which records participate.
- Configurable duplicate handling with strict failure by default.
- Exact RFC 6901 object-member ignores.
- Semantic number comparison without binary floating-point rounding.
- Deterministic summaries and changed-identity iteration.
- Original OLD and NEW physical line numbers for every change.
- Local, HTTP/HTTPS, file-like, and supported compressed sources through [`py-jsonl`](https://github.com/rmoralespp/jsonl).
- The same comparison engine through the CLI and Python API.
- Disk-backed comparison with configurable `jsonl-diff` temporary storage.

## Requirements and installation

`jsonl-diff` supports Python 3.8 through 3.14.

```bash
python -m pip install jsonl-diff
```

## CLI quick start

Given:

```jsonl
{"id":1,"name":"Ada"}
{"id":2,"name":"Grace"}
```

and a reordered, changed dataset:

```jsonl
{"id":2,"name":"Grace Hopper"}
{"name":"Ada","id":1}
{"id":3,"name":"Linus"}
```

compare them by `id`:

```bash
jsonl-diff old.jsonl new.jsonl --key id
```

Standard output contains only the text summary:

```text
Records:
  equal:     1
  added:     1
  deleted:   0
  modified:  1
```

The result means one record is unchanged, one was added, and one existing
record changed, regardless of physical order (see
[Determinism](#determinism)).

Diagnostics are written to standard error. Use `--quiet` when only the exit
status or details file is needed.

### Common pipelines

Filter records with `--where` instead of preprocessing input externally; it
runs once per source, inside the same disk-backed pipeline, with no extra
process or intermediate file:

```bash
jsonl-diff old.jsonl new.jsonl \
  --key id \
  --where 'deleted_at == `null`'
```

Read compressed input directly when no preprocessing is required:

```bash
jsonl-diff old.jsonl.xz new.jsonl.gz --key id
```

For a quick spot-check on the first N records of two large files on
Bash-compatible systems, use process substitution:

```bash
jsonl-diff \
  <(head -n 1000 old.jsonl) \
  <(head -n 1000 new.jsonl) \
  --key id
```

`-` represents stdin. Both inputs cannot use the same stdin stream.

### CLI syntax and options

```text
jsonl-diff [-h] --key KEY [--ignore IGNORE] [--where EXPRESSION]
           [--duplicates {error,first,last}] [--details FILE] [--quiet]
           [--max-temp MAX_TEMP]
           old new
```

| Argument                | Meaning                                                                                                                                                                                            |
|-------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `old`                   | OLD local path, HTTP/HTTPS source, or `-` for stdin.                                                                                                                                               |
| `new`                   | NEW local path, HTTP/HTTPS source, or `-` for stdin.                                                                                                                                               |
| `--key KEY`             | Required top-level identity field. Repeat it or use a comma-separated value for a composite identity.                                                                                              |
| `--ignore JSON_POINTER` | Exact RFC 6901 pointer to an object member to remove before content comparison. Repeatable.                                                                                                        |
| `--where EXPRESSION`    | [JMESPath](https://jmespath.org/) expression evaluated against each raw record. Only records for which it is truthy participate in the comparison; the rest behave as if absent from both sources. |
| `--duplicates POLICY`   | Handle repeated identities with `error` (default), `first`, or `last`.                                                                                                                             |
| `--details FILE`        | Write machine-readable JSONL details to `FILE`; details are never written to stdout.                                                                                                               |
| `--quiet`               | Suppress the normal stdout summary. Errors still go to stderr.                                                                                                                                     |
| `--max-temp MAX_TEMP`   | Limit storage owned by `jsonl-diff` to a positive integer number of bytes.                                                                                                                         |
| `-h`, `--help`          | Show command help and exit.                                                                                                                                                                        |

These composite-key forms are equivalent:

```bash
jsonl-diff old.jsonl new.jsonl --key country,customerId,type

jsonl-diff old.jsonl new.jsonl \
  --key country \
  --key customerId \
  --key type
```

Ignore volatile fields by exact path:

```bash
jsonl-diff old.jsonl new.jsonl \
  --key id \
  --ignore /updated_at \
  --ignore /metadata/request_id
```

`--where` combines freely with `--key` and `--ignore` in the same invocation;
see [Common pipelines](#common-pipelines) for an example and [Filtering with `--where`](#filtering-with---where) 
for its semantics and literal syntax.

### Exit codes

| Code | Meaning                                                                    |
|-----:|----------------------------------------------------------------------------|
|  `0` | The inputs are equal under the configured rules.                           |
|  `1` | At least one added, deleted, or modified identity was found.               |
|  `2` | A handled input, source, resource, output, or interruption error occurred. |
|  `3` | CLI usage or comparison configuration is invalid.                          |

Invalid argparse usage exits directly with code `3`. The Python API uses
exceptions instead of these process exit codes.

## Details JSONL

`--details FILE` streams a deterministic JSONL report through `py-jsonl` after
both inputs have been completely indexed and validated:

```bash
jsonl-diff old.jsonl new.jsonl --key id --details changes.jsonl
```

For an OLD source containing identities `1` and `2`, and a NEW source
containing changed identity `2` and added identity `3`, the file is:

```jsonl
{"duplicates":"error","ignore":[],"key":["id"],"type":"meta","where":null}
{"key":[1],"old_line":1,"op":"deleted","type":"change"}
{"key":[2],"new_line":1,"old_line":2,"op":"modified","type":"change"}
{"key":[3],"new_line":2,"op":"added","type":"change"}
{"added":1,"deleted":1,"equal":0,"modified":1,"type":"summary"}
```

Record types are:

- **`meta`**: always first. `key` contains normalized identity-field names;
  `ignore` contains the configured ignore pointers; `where` contains the
  configured JMESPath expression, or `null` when `--where` was not used.
- **`change`**: one per changed identity. `op` is `added`, `deleted`, or
  `modified`. `old_line` is present for deletions and modifications;
  `new_line` is present for additions and modifications. Equal records are not
  emitted.
- **`summary`**: always last after a successful write, with all four totals.

The normal stdout summary is unchanged when `--details` is used, unless
`--quiet` suppresses it. A write failure can leave a partial details file and
returns exit code `2`.

## Python API

### Comparing sources

```python
from jsonl_diff import ChangeOperation, diff

with diff(
        "old.jsonl.gz",
        "new.jsonl.gz",
        key=("country", "customerId"),
        ignore=("/updated_at",),
        where="country == `\"ES\"`",
        duplicates="error",
        max_temp=2_000_000_000,
) as result:
    print(result.summary)

    for change in result.changes(ChangeOperation.MODIFIED):
        print(change.key, change.old_line, change.new_line)
```

The callable signature is:

```python
from typing import Any, Optional, Sequence, Union

from jsonl_diff import DiffResult, DuplicatePolicy


def diff(
        old: Any,
        new: Any,
        *,
        key: Union[str, Sequence[str]],
        ignore: Sequence[str] = (),
        where: Optional[str] = None,
        duplicates: Union[str, DuplicatePolicy] = DuplicatePolicy.ERROR,
        max_temp: Optional[int] = None,
) -> DiffResult:
    ...
```

`diff()` creates a comparison session without opening either source. Entering
its context fully reads, indexes, and validates both sources before exposing
the result. `DiffResult.changes()` is then a lazy iterator over the disk-backed
result rather than a list held in memory.

Using `DiffResult` as a context manager is required. It owns the temporary
resources and removes its private workspace on exit. A result cannot be entered
more than once. Its summary becomes available after entering and remains
available after closing; iterating changes requires the context to remain open.

### Result models

`result.summary` is an immutable `Summary`:

```python
Summary(equal=10, added=2, deleted=1, modified=3)
```

It exposes integer fields `equal`, `added`, `deleted`, and `modified`, plus the
boolean property `different`. The same values are available directly as
read-only `DiffResult` properties.

Each item from `result.changes()` is an immutable `Change` with:

- `operation`: a `ChangeOperation` enum value (`ADDED`, `DELETED`, or
  `MODIFIED`);
- `key`: a typed tuple in normalized identity-field order;
- `old` and `new`: optional `SourceLocation` values;
- `old_line` and `new_line`: convenience properties returning a one-based
  physical line or `None`.

For additions, `old`/`old_line` are `None`; for deletions,
`new`/`new_line` are `None`; modifications have both locations. Location
sources are labeled `OLD` and `NEW`.

Pass a `ChangeOperation` to filter without materializing all changes:

```python
with diff("old.jsonl", "new.jsonl", key="id") as result:
    added = result.changes(ChangeOperation.ADDED)
    for change in added:
        print(change.key, change.new_line)
```

With numeric keys, API key components are `decimal.Decimal` values. For
example, JSON identity `7` is returned as `Decimal("7")`; JSON strings and
booleans retain their types. Details JSONL writes numeric keys as JSON numbers,
including arbitrary-precision integers.

### API errors

The public error hierarchy starts with `JsonlDiffError`:

- `ConfigurationError`: invalid keys, ignore pointers, or `max_temp`;
- `InputError`: an invalid source or record; exposes `source` and optional
  `line`;
- `DuplicateKeyError`: an `InputError` with `key` and the first/repeated
  physical lines in `lines`;
- `ResourceError`: the temporary index cannot be created, written, or kept
  within its configured limit.

Failures during `diff()` clean up the workspace before the exception is
raised.

## Sources and compression

Source opening and decompression are delegated to `py-jsonl`:

| Source             |       CLI        |           Python API            |
|--------------------|:----------------:|:-------------------------------:|
| Local path         |       Yes        | Yes, including path-like values |
| HTTP/HTTPS URL     |       Yes        |               Yes               |
| File-like object   |        No        |               Yes               |
| gzip (`.gz`)       |       Yes        |               Yes               |
| bzip2 (`.bz2`)     |       Yes        |               Yes               |
| xz (`.xz`)         |       Yes        |               Yes               |
| Zstandard (`.zst`) | Python 3.14 only |        Python 3.14 only         |
| ZIP archive        |        No        |               No                |

Zstandard availability follows `py-jsonl` and its use of Python 3.14's
standard-library zstd support; it is not supported by this project on earlier
Python versions. Compression and HTTP transfer boundaries do not affect the
reported decoded JSONL line numbers.

## Comparison semantics

### Strict JSONL records

- Every physical line must be non-blank, valid JSON containing exactly one
  top-level object.
- Local paths and binary streams are decoded as UTF-8. Text streams are
  already decoded, while URL response charset handling follows `py-jsonl`
  (defaulting to UTF-8 when no charset is declared).
- Blank lines, malformed JSON, duplicate object property names, `NaN`,
  `Infinity`, and `-Infinity` are errors.
- Top-level arrays, strings, numbers, booleans, and `null` are rejected.
- Input errors identify `OLD` or `NEW` and include the physical line when
  available.

No normal summary or details output begins until both inputs pass complete
input and duplicate validation.

### Identity and duplicates

Identity fields are top-level object-member names. Each component must exist
and be a string, number, boolean, or (composite identities only) `null`;
objects and arrays are invalid. A single-field identity may not be `null`,
since that would collapse every `null` record into one indistinguishable
identity; a `null` value is only tolerated as one component of a composite (multi-field) identity, where the other
components still keep the key
selective. Nested identity paths and automatic key detection are not
supported.

Composite identity field names are sorted lexically before their values are
extracted. Consequently, `key=("b", "a")` and `key=("a", "b")` both produce
keys in `(a, b)` order and match identically. Repeated or empty identity-field
names are configuration errors.

JSON types remain significant: `"1"` is different from `1`, and `true` is
different from `1`.

Every normalized identity must be unique within OLD and within NEW by default.
The first duplicate aborts the comparison; `DuplicateKeyError` reports the
first occurrence and repeated physical line. Set `duplicates="first"` to keep
the first occurrence or `duplicates="last"` to keep the last occurrence and
its physical line. These tolerant policies depend on physical input order.

### Ignored object members

Ignore expressions are exact
[RFC 6901 JSON Pointers](https://www.rfc-editor.org/rfc/rfc6901). Escapes such
as `~1` for `/` and `~0` for `~` are supported. An absent path is harmless,
and `/updated_at` does not match `/metadata/updated_at`.

Ignores are applied symmetrically before content canonicalization. A pointer
may remove an entire array-valued object member, but it may not traverse an
array or address an array element. Ignoring a top-level identity field is also
invalid. Wildcards, JSONPath, and recursive name matching are not supported.

### Filtering with `--where`

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

### Canonical content and numbers

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

### Determinism

Summary counts do not depend on either source's physical record order.
Changes, including details records, use the stable lexical byte order of each
canonical typed identity. This is a reproducibility guarantee, not numeric,
locale-aware, or human-oriented sorting.

## Disk-backed architecture and temporary files

Each record is parsed and validated incrementally, normalized, and inserted
into a private SQLite database under an operating-system temporary directory.
The database stores the typed canonical identity, original line, content
length, and SHA-256 digest; the full canonical bytes are not persisted. A
uniqueness constraint detects duplicate identities. SQL joins calculate the
summary, and ordered SQLite cursors drive lazy change iteration.

This architecture bounds memory by the records currently being processed and
database buffers; it does not keep the complete decoded inputs or all changes
in RAM. It does require temporary disk space. The identity, line, length, and
digest index remains until the `DiffResult` is closed, so temporary usage
scales with the number of records rather than the combined input size.

`max_temp` / `--max-temp` accepts a positive byte count. The implementation
limits and checks files in the workspace owned by `jsonl-diff`, raising
`ResourceError` (CLI exit `2`) when the index cannot stay within that budget.
Choose a limit with room for SQLite pages and index overhead.

`py-jsonl` may create its own temporary staging files for remote or compressed
sources. Those files follow `py-jsonl`'s resource policy and are not counted by
`jsonl-diff`'s `max_temp` limit. Total system temporary usage can therefore
exceed the configured value.

The private workspace is removed on context-manager exit, explicit `close()`,
or a handled failure during construction. Cleanup failures are emitted as
warnings rather than replacing the primary error.

## Limitations and non-goals

`jsonl-diff` intentionally does not provide:

- line- or physical-order comparison;
- equal-record events or complete record payloads in details output;
- field-level diffs, JSON Patch, move detection, or rename detection;
- nested identities, fuzzy matching, or key autodetection;
- numeric tolerances, unordered-array comparison, or Unicode normalization;
- wildcard/JSONPath ignores or ignores that traverse arrays;
- schema validation or repair of malformed input;
- ZIP input, database-table input, cloud-provider SDK integrations, GUI, or
  HTML reports.

It is a reconciliation tool for strict JSONL datasets, not a general-purpose
visual JSON diff.

## Development

Clone the repository and sync the test and lint dependency groups with `uv`:

```bash
uv sync --group test --group lint
```

Run the test suite and Ruff:

```bash
uv run pytest
uv run ruff check --quiet --output-format=concise .
```

The tests cover CLI behavior, identity and canonicalization rules, strict
validation, details output, source types, compression, temporary limits, and
resource cleanup.

## License

See [LICENSE](LICENSE).
