# Disk-backed architecture and temporary files

[← Back to README](../README.md)

How `jsonl-diff` indexes records, bounds memory, limits temporary disk usage,
and delegates source/compression handling.

## SQLite index

Each input is parsed and validated incrementally in a separate worker process.
Every worker writes a private SQLite database under an operating-system
temporary directory. After both workers finish, the parent merges those
databases into the final index used for comparison. See
[Parallel indexing](parallel-indexing.md) for the process lifecycle and error
handling.

The index stores the typed canonical identity, original line, content length,
and SHA-256 digest; the full canonical bytes are not persisted. A uniqueness
constraint detects duplicate identities. When `first` or `last` tolerates a
collision, a second table stores the discarded occurrence's identity, physical
line, canonical length, and digest. SQL joins calculate the summary, and
ordered SQLite cursors drive lazy change and duplicate iteration.

`msgspec` produces both the stored identity bytes and the canonical content
bytes with recursively sorted object keys. Numeric scale and exponent
representation are preserved, so representationally different numeric
identities occupy different index entries.

This architecture bounds memory by the records currently being processed and
database buffers; it does not keep the complete decoded inputs or all changes
in RAM. It does require temporary disk space. The identity, line, length, and
digest index remains until the `DiffResult` is closed, so temporary usage
scales with the number of selected records plus tolerated duplicate
occurrences rather than the combined input size for path and URL sources.
File-like objects and stdin are first copied into the workspace so both workers
can reopen them, so those sources temporarily require their full encoded size
in addition to the indexes. Duplicate diagnostics compare stored fingerprints
and do not require retaining or rereading full records.

## Observed-schema profile

When `schema_diff=True` / `--schema-diff` is enabled, indexing also builds an
observed-schema profile in the same SQLite workspace. The profile stores one
aggregate row per source and RFC 6901 field path, plus object-occurrence counts
used to distinguish a missing field from an explicit `null`.

Per-record observations are accumulated in small Python dictionaries and
flushed to SQLite every 1024 physical lines. SQLite upserts add the batch
counts, so profiling does not perform one database write for every field in
every record. Memory scales with the distinct paths in the current batch and
record; disk usage scales with distinct observed paths rather than the number
of records. Datasets with dynamic property names can still create large
profiles.

Schema profiling happens during the original input pass and does not retain
complete records or reread a source. It therefore works with stdin, remote,
and compressed sources. Nested objects are traversed iteratively. Arrays are
profiled as terminal `array` values; their elements are not inferred.

## Cleanup

Worker databases are removed immediately after a successful merge or failed
parallel attempt. The complete private workspace, including staged stream
inputs and the final index, is removed on context-manager exit, explicit
`close()`, or a handled failure during construction. Cleanup failures are
emitted as warnings rather than replacing the primary error.

## Sources and compression

Source opening and decompression are delegated to
[`py-jsonl`](https://github.com/rmoralespp/jsonl).
JSONL input is decoded by `py-jsonl`; `--format json` opens the same
decompressed byte stream with `py-jsonl.open_stream()` and parses top-level
array elements incrementally with `ijson`.

| Source             |       CLI        |           Python API            |
|---------------------|:----------------:|:--------------------------------:|
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
reported decoded JSONL line numbers. For `--format json`, the corresponding
locations are one-based array-element ordinals rather than physical lines.
