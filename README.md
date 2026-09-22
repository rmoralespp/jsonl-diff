# jsonl-diff

<p>
  <img src="https://img.shields.io/pypi/v/jsonl-diff.svg" alt="PyPI">
  <img src="https://img.shields.io/pypi/pyversions/jsonl-diff.svg" alt="Python">
  <img src="https://github.com/rmoralespp/jsonl-diff/workflows/CI/badge.svg" alt="CI">
  <img src="https://codecov.io/gh/rmoralespp/jsonl-diff/branch/main/graph/badge.svg" alt="Coverage">
  <img src="https://img.shields.io/github/license/rmoralespp/jsonl-diff.svg" alt="License">
</p>

Compare large JSONL/NDJSON datasets **by record identity instead of line
position**, without loading the complete inputs into memory.

`jsonl-diff` matches records using one or more top-level fields and reports:
`equal`, `added`, `deleted`, `modified`, and tolerated duplicates in each
source.

It is useful for snapshots, ETL validation, migrations, exports, and CI checks.

> For line-level, field-level, JSON Patch, or visual diffs, use a tool designed for those purposes instead.

## Installation

```bash
pip install jsonl-diff
```

## Quick start

Given:

```jsonl
{"id":1,"name":"Ada"}
{"id":2,"name":"Grace"}
```

and:

```jsonl
{"id":2,"name":"Grace Hopper"}
{"name":"Ada","id":1}
{"id":3,"name":"Linus"}
```

compare them by `id`:

```bash
jsonl-diff old.jsonl new.jsonl --key id
```

```text
Records:
  equal:     1
  added:     1
  deleted:   0
  modified:  1
  OLD duplicates:  0
  NEW duplicates:  0
```

Record order does not matter.

## Features

* Single or composite top-level identities.
* Disk-backed comparison for large datasets ([architecture](https://github.com/rmoralespp/jsonl-diff/blob/main/docs/architecture.md)).
* Optional **JMESPath** filtering with `--where`.
* Configurable duplicate handling with counts and diagnostics: `error`, `first`, or `last`.
* Exact **RFC 6901** JSON Pointer ignores.
* Semantic number comparison using `Decimal`.
* Deterministic summaries and change iteration.
* Original OLD/NEW physical line numbers.
* Machine-readable JSONL change log with `--details` ([format](https://github.com/rmoralespp/jsonl-diff/blob/main/docs/details-format.md)).
* Local, HTTP/HTTPS, file-like, and supported compressed sources.
* CLI and Python API using the same comparison engine.

## CLI

```text
jsonl-diff [-h] --key KEY [--ignore IGNORE] [--where EXPRESSION]
           [--duplicates {error,first,last}] [--details FILE] [--quiet]
           [--max-temp MAX_TEMP]
           old new
```

| Option                | Description                                                                    |
| --------------------- | ------------------------------------------------------------------------------ |
| `old`, `new`          | Local path, HTTP/HTTPS source, or `-` for stdin                                |
| `--key KEY`           | Required top-level identity field; repeat or comma-separate for composite keys |
| `--ignore POINTER`    | RFC 6901 pointer to exclude from content comparison                            |
| `--where EXPRESSION`  | JMESPath filter applied to each record                                         |
| `--duplicates POLICY` | `error` (default), or select and report duplicates with `first`/`last`          |
| `--details FILE`      | Write deterministic machine-readable JSONL changes                             |
| `--quiet`             | Suppress the normal summary                                                    |
| `--max-temp BYTES`    | Best-effort budget for `jsonl-diff` workspace temporary storage                |

Examples:

```bash
# Composite identity
jsonl-diff old.jsonl new.jsonl --key country,customerId,type

# Ignore volatile fields
jsonl-diff old.jsonl new.jsonl \
  --key id \
  --ignore /updated_at \
  --ignore /metadata/request_id

# Filter records
jsonl-diff old.jsonl new.jsonl \
  --key id \
  --where 'deleted_at == `null`'

# Compressed input
jsonl-diff old.jsonl.xz new.jsonl.gz --key id
```

### Exit codes

| Code | Meaning                                                    |
| ---: | ---------------------------------------------------------- |
|  `0` | Inputs are equal and contain no tolerated duplicates       |
|  `1` | Record differences or tolerated duplicate identities found |
|  `2` | Input, resource, output, or runtime error                  |
|  `3` | Invalid CLI configuration or usage                         |

## Python API

```python
from jsonl_diff import ChangeOperation, diff

with diff(
    "old.jsonl.gz",
    "new.jsonl.gz",
    key=("country", "customerId"),
    ignore=("/updated_at",),
    where='country == `"ES"`',
) as result:
    print(result.summary)

    for change in result.changes(ChangeOperation.MODIFIED):
        print(change.key, change.old_line, change.new_line)
```

`diff()` returns a disk-backed `DiffResult`, used as a context manager.
Results are streamed lazily through `changes()` rather than materialized in
memory.

The main result models are:

```python
Summary(equal=10, added=2, deleted=1, modified=3)
```

and immutable `Change` objects containing the operation, typed identity, and
OLD/NEW source locations.

The public exception hierarchy is rooted at `JsonlDiffError`:

* `ConfigurationError`
* `InputError`
* `DuplicateKeyError`
* `ResourceError`

See the [Python API reference](https://github.com/rmoralespp/jsonl-diff/blob/main/docs/python-api.md) for the full callable
signature, result models, `Decimal` key semantics, and error hierarchy.

## Comparison semantics

`jsonl-diff` is intentionally strict:

* Every input line must contain exactly one valid top-level JSON object.
* Blank lines, malformed JSON, duplicate properties, `NaN`, and infinities are rejected.
* Identity fields must be top-level scalar values (`null` allowed only as one component of a composite key).
* Identity types remain significant: `"1"` ≠ `1`, `true` ≠ `1`.
* Duplicate identities fail by default; `--duplicates first`/`last` select one
  occurrence, report every discarded occurrence, and return exit code `1`.
* Object property order is ignored; array order is significant.
* Numbers are compared by mathematical value: `1`, `1.0`, and `1e0` are equal.
* Unicode strings are compared without normalization.
* Changes are reported in deterministic identity order.

`--where` selects which records participate; `--key` defines identity;
`--ignore` removes fields from content comparison. See
[Comparison semantics](https://github.com/rmoralespp/jsonl-diff/blob/main/docs/comparison-semantics.md) for the full rules,
including duplicate handling, ignore-pointer edge cases, `--where` evaluation
order, and canonical number formatting.

## Sources & compression

Supported sources include local paths and HTTP/HTTPS URLs. The Python API
also accepts file-like objects.

Supported compression:

* gzip
* bzip2
* xz
* Zstandard on Python 3.14

See [architecture](https://github.com/rmoralespp/jsonl-diff/blob/main/docs/architecture.md) for the full source/compression
support matrix and how sources are delegated to `py-jsonl`.

## Limitations

`jsonl-diff` does **not** provide:

* line/position-based diffs
* field-level diffs or JSON Patch
* move/rename detection
* nested identities or automatic key detection
* fuzzy matching or numeric tolerances
* unordered-array comparison
* schema validation or input repair
* ZIP, database, or cloud-provider inputs
* GUI or HTML reports

It is a **dataset reconciliation tool**, not a general-purpose visual JSON diff.

## Further reading

* [Comparison semantics](https://github.com/rmoralespp/jsonl-diff/blob/main/docs/comparison-semantics.md) — identity, duplicates, ignores, `--where`, canonical numbers, determinism.
* [Disk-backed architecture](https://github.com/rmoralespp/jsonl-diff/blob/main/docs/architecture.md) — SQLite index, `max_temp`, cleanup, sources and compression.
* [Details JSONL format](https://github.com/rmoralespp/jsonl-diff/blob/main/docs/details-format.md) — machine-readable `--details` output schema.
* [Python API reference](https://github.com/rmoralespp/jsonl-diff/blob/main/docs/python-api.md) — full signature, result models, and error hierarchy.

## Development

```bash
uv sync --group test --group lint

uv run pytest
uv run ruff check --quiet --output-format=concise .
```

The test suite covers CLI behavior, identity/canonicalization, validation, details output, sources, compression, temporary limits, and cleanup.

## License

See [LICENSE](LICENSE).
