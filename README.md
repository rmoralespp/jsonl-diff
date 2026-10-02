<div align="center">

# jsonl-diff

### A lightweight Python CLI and library for identity-aware JSON diffs

Compare large JSONL, NDJSON, and top-level JSON arrays by record identity—not
by line position—without loading either complete dataset into memory. Strict,
deterministic, and disk-backed.

[![PyPI](https://img.shields.io/pypi/v/jsonl-diff?style=flat-square&color=0A7BBB)](https://pypi.org/project/jsonl-diff/)
[![Python](https://img.shields.io/pypi/pyversions/jsonl-diff?style=flat-square)](https://pypi.org/project/jsonl-diff/)
[![CI](https://img.shields.io/github/actions/workflow/status/rmoralespp/jsonl-diff/ci.yml?branch=main&style=flat-square&label=CI)](https://github.com/rmoralespp/jsonl-diff/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/codecov/c/github/rmoralespp/jsonl-diff?style=flat-square)](https://codecov.io/gh/rmoralespp/jsonl-diff)
[![License](https://img.shields.io/github/license/rmoralespp/jsonl-diff?style=flat-square)](LICENSE)

[Install](#install) · [Quick start](#quick-start) · [CLI](#cli) · [Python API](#python-api) · [Semantics](#comparison-semantics) · [Documentation](#documentation)

</div>

---

`jsonl-diff` reconciles two datasets using one or more top-level identity
fields. Record order does not matter. It reports what is equal, added, deleted,
modified, or duplicated while preserving the original OLD and NEW locations.

```text
                     identity
OLD ── parse ── index ───┬─── compare ── summary / changes
NEW ── parse ── index ───┘
```

## Why jsonl-diff

|                             |                                                                                                  |
|-----------------------------|--------------------------------------------------------------------------------------------------|
| **Scales beyond memory**    | Streams both inputs and keeps the comparison index in SQLite.                                    |
| **Matches real datasets**   | Compares records by single or composite identity instead of line number.                         |
| **Stays deterministic**     | Produces stable summaries, change order, and machine-readable details.                           |
| **Treats data strictly**    | Rejects malformed input, invalid identities, and duplicates by default.                          |
| **Uses all available work** | Indexes OLD and NEW concurrently in isolated worker processes.                                   |
| **Fits pipelines**          | Supports filtering, ignored fields, compressed sources, schema drift, and meaningful exit codes. |

## Install

```bash
pip install jsonl-diff
```

Requires **Python 3.10+**.

## Quick start

Given `old.jsonl`:

```jsonl
{"id":1,"name":"Ada"}
{"id":2,"name":"Grace"}
```

and `new.jsonl`:

```jsonl
{"id":2,"name":"Grace Hopper"}
{"name":"Ada","id":1}
{"id":3,"name":"Linus"}
```

compare by `id`:

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

Property order and record order do not affect the result.

## Core capabilities

- Single or composite top-level identities.
- Incremental JSONL/NDJSON and top-level JSON-array parsing.
- Disk-backed, parallel indexing for large datasets.
- Exact RFC 6901 ignore pointers.
- JMESPath record filtering.
- Configurable duplicate handling: `error`, `first`, or `last`.
- Representation-sensitive numeric comparison.
- Observed-schema drift for fields, types, nullability, and requiredness.
- Deterministic JSONL details output.
- Local, HTTP/HTTPS, file-like, and compressed sources.
- CLI and Python API backed by the same comparison engine.

## CLI

```text
jsonl-diff [-h] --key KEY [--ignore IGNORE] [--where EXPRESSION]
           [--duplicates {error,first,last}] [--missing-key {error,null}]
           [--details FILE] [--quiet]
           [--schema-diff] [--schema-ignore POINTER] [--format {jsonl,json}]
           old new
```

| Option                    | Purpose                                                               |
|---------------------------|-----------------------------------------------------------------------|
| `old`, `new`              | Local path, HTTP/HTTPS source, or `-` for stdin                       |
| `--key KEY`               | Top-level identity field; repeat or comma-separate for composite keys |
| `--ignore POINTER`        | RFC 6901 field pointer excluded from content comparison               |
| `--where EXPRESSION`      | JMESPath filter evaluated against each record                         |
| `--duplicates POLICY`     | Reject duplicates or retain the `first`/`last` occurrence             |
| `--missing-key POLICY`    | Reject absent identity fields or treat them as `null`                 |
| `--details FILE`          | Write deterministic machine-readable JSONL changes                    |
| `--schema-diff`           | Compare observed fields, types, nullability, and requiredness         |
| `--schema-ignore POINTER` | Exclude a pointer from observed-schema profiling                      |
| `--format FORMAT`         | Read `jsonl` (default) or one top-level JSON array with `json`        |
| `--quiet`                 | Suppress the human-readable summary                                   |

### Common workflows

**Composite identity**

```bash
jsonl-diff old.jsonl new.jsonl --key country,customerId,type
```

**Ignore volatile fields**

```bash
jsonl-diff old.jsonl new.jsonl \
  --key id \
  --ignore /updated_at \
  --ignore /metadata/request_id
```

**Filter participating records**

```bash
jsonl-diff old.jsonl new.jsonl \
  --key id \
  --where 'deleted_at == `null`'
```

**Allow a missing composite-key component**

```bash
jsonl-diff old.jsonl new.jsonl \
  --key country,customerId \
  --missing-key null
```

**Detect observed-schema drift**

```bash
jsonl-diff old.jsonl new.jsonl \
  --key id \
  --schema-diff \
  --schema-ignore /metadata
```

**Write a machine-readable change log**

```bash
jsonl-diff old.jsonl new.jsonl \
  --key id \
  --details changes.jsonl \
  --quiet
```

Using the files from the quick start, `changes.jsonl` contains:

```jsonl
{"duplicates":"error","ignore":[],"key":["id"],"missing_key":"error","type":"meta","where":null}
{"key":[2],"new_line":1,"old_line":2,"op":"modified","type":"change"}
{"key":[3],"new_line":3,"op":"added","type":"change"}
{"added":1,"deleted":0,"equal":1,"modified":1,"new_duplicates":0,"old_duplicates":0,"type":"summary"}
```

The stream starts with comparison metadata, emits only changed identities in
deterministic order, and finishes with the complete summary. See the
[details format](docs/details-format.md) for duplicate and schema events.

**Read compressed inputs**

```bash
jsonl-diff old.jsonl.xz new.jsonl.gz --key id
```

**Read top-level JSON arrays**

```bash
jsonl-diff old.json new.json --key id --format json
```

### Exit codes

| Code | Meaning                                                     |
|-----:|-------------------------------------------------------------|
|  `0` | No record, duplicate, or requested schema differences       |
|  `1` | Record differences, tolerated duplicates, or schema changes |
|  `2` | Input, resource, output, or runtime failure                 |
|  `3` | Invalid configuration or CLI usage                          |

## Python API

```python
from jsonl_diff import ChangeOperation, diff


def main():
    with diff(
            "old.jsonl.gz",
            "new.jsonl.gz",
            key=("country", "customerId"),
            ignore=("/updated_at",),
            where='country == `"ES"`',
            schema_diff=True,
    ) as result:
        print(result.summary)

        for change in result.changes(ChangeOperation.MODIFIED):
            print(change.key, change.old_line, change.new_line)

        for change in result.schema_changes():
            print(change.operation, change.path)


if __name__ == "__main__":
    main()
```

`diff()` returns a context-managed, disk-backed `DiffResult`. Entering the
context completely validates and indexes both sources. Changes, duplicates,
and schema changes are then iterated lazily instead of being materialized in
memory.

Applications must protect the entry point because indexing uses worker
processes. See [Parallel indexing](docs/parallel-indexing.md#application-entry-points)
for the multiprocessing requirements.

The immutable result models include:

- `Summary` and `SchemaSummary`
- `Change` and `SourceLocation`
- `Duplicate`
- `SchemaChange` and `SchemaFieldProfile`

The public exception hierarchy is rooted at `JsonlDiffError`:

- `ConfigurationError`
- `InputError`
- `DuplicateKeyError`
- `ResourceError`

See the [Python API reference](docs/python-api.md) for the complete signature,
models, iterator behavior, numeric key semantics, and error hierarchy.

## Comparison semantics

The comparison is intentionally strict:

- Each JSONL line must contain exactly one valid top-level object.
- `--format json` requires one top-level array whose elements are records.
- Blank lines, malformed JSON, `NaN`, and infinities are rejected.
- Repeated object properties use last-wins semantics.
- Identity fields are top-level JSON values; nested identity paths are not supported.
- Object member order is ignored recursively; array order remains significant.
- JSON types remain distinct: `"1"` is not `1`, and `true` is not `1`.
- Parsed numeric representation remains significant: `1` is not `1.0`, and
  `1.0` is not `1.00`.
- Unicode strings are compared without normalization.
- Duplicate identities fail unless `first` or `last` is explicitly selected.
- Changes are emitted in deterministic canonical-identity order.

`--where` decides which records participate. `--key` defines their identity.
`--ignore` excludes fields from content comparison. `--schema-ignore`
independently excludes fields from observed-schema profiling.

Read [Comparison semantics](docs/comparison-semantics.md) for duplicate rules,
ignore-pointer edge cases, filter order, schema inference, and numeric behavior.

## Sources and compression

| Source             |     CLI     | Python API  |
|--------------------|:-----------:|:-----------:|
| Local path         |     Yes     |     Yes     |
| HTTP/HTTPS URL     |     Yes     |     Yes     |
| File-like object   |     No      |     Yes     |
| gzip (`.gz`)       |     Yes     |     Yes     |
| bzip2 (`.bz2`)     |     Yes     |     Yes     |
| xz (`.xz`)         |     Yes     |     Yes     |
| Zstandard (`.zst`) | Python 3.14 | Python 3.14 |
| ZIP archive        |     No      |     No      |

Source opening and decompression are delegated to `py-jsonl`. File-like inputs
and stdin are staged in the private temporary workspace so both indexing
workers can reopen them.

## Scope

`jsonl-diff` is a dataset reconciliation tool—not a visual or structural JSON
editor. It intentionally does not provide:

- line-position or field-level diffs;
- JSON Patch output;
- move or rename detection;
- nested identities or automatic key detection;
- fuzzy matching or numeric tolerances;
- unordered-array comparison;
- declared-schema validation or input repair;
- ZIP, database, or cloud-provider inputs;
- GUI or HTML reports.

## Documentation

| Guide                                                | Covers                                                             |
|------------------------------------------------------|--------------------------------------------------------------------|
| [Comparison semantics](docs/comparison-semantics.md) | Identity, duplicates, ignores, filtering, numbers, and determinism |
| [Disk-backed architecture](docs/architecture.md)     | SQLite indexes, temporary storage, cleanup, and source handling    |
| [Parallel indexing](docs/parallel-indexing.md)       | Worker lifecycle, database merge, recovery, and entry points       |
| [Details format](docs/details-format.md)             | Machine-readable JSONL event contract                              |
| [Python API](docs/python-api.md)                     | Complete signature, models, iterators, and errors                  |

## Development

```bash
uv sync --group test --group lint
uv run pytest
uv run ruff check --quiet --output-format=concise .
```

The suite covers CLI behavior, identity and canonicalization, validation,
details output, source handling, compression, parallel indexing, and cleanup.

## License

Licensed under the terms in [LICENSE](LICENSE).
