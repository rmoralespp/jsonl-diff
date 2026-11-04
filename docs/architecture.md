# Disk-backed architecture and temporary files

[← Back to README](../README.md)

How `jsonl-diff` indexes records, bounds memory, uses temporary disk space, and
delegates source/compression handling.

## SQLite index

Each input is parsed and validated incrementally in a separate worker process.
Every worker writes a private SQLite database under an operating-system
temporary directory. After both workers finish, the parent merges those
databases into the final index used for comparison. See
[Parallel indexing](parallel-indexing.md) for the process lifecycle and error
handling.

The index stores the typed canonical identity, original line, content length,
and SHA-256 digest; the full canonical bytes are not persisted, except for
modified records when field diff is enabled (see
[Field-diff reread](#field-diff-reread)). A uniqueness
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
and the field-diff reread can reopen them, so those sources temporarily require their full encoded size
in addition to the indexes. Duplicate diagnostics compare stored fingerprints
and do not require retaining or rereading full records.

## Observed-schema profile

When `schema_diff=True` / `--schema-diff` is enabled, indexing also builds an
observed-schema profile in the same SQLite workspace. The profile stores one
aggregate row per source and RFC 6901 field path, plus object-occurrence counts
used to distinguish a missing field from an explicit `null`.

Observations are accumulated in memory and flushed to SQLite every 1024
physical lines, so profiling does not write once per field per record. Memory
scales with the distinct paths in the current batch; disk usage scales with
distinct observed paths rather than the number of records. Datasets with
dynamic property names can still create large profiles.

Schema profiling happens during the original input pass and does not retain
complete records or reread a source. It therefore works with stdin, remote,
and compressed sources. Nested objects are traversed iteratively. Arrays are
profiled as terminal `array` values; their elements are not inferred.

## Field-diff reread

The index stores fingerprints rather than records, so field diff adds a
sequential reread of both sides after the summary. Only modified lines are
decoded; their canonical body (after `--ignore`) must match the indexed length
and digest, otherwise entering the context fails with `InputError`
(`source changed during comparison`). Every line is still read, compressed
sources are decompressed again, `--format json` is fully reparsed, and URLs are
downloaded again; staged stdin and file-like inputs use the workspace copy.

Verified bodies are stored in a temporary table indexed by side and identity
(about twice the canonical size of the modified records), flushed in batches of
at most 2048 bodies or 8 MiB of canonical bytes. A single larger body is
flushed on its own, so this is not a total RAM limit. `changes()` computes each
patch lazily, comparing undecoded members as bytes and decoding only changed
values. `field_summary` totals are merged into a table keyed by path, so
dynamic property names spill to disk instead of growing memory. The totals are
built once per result, during the details pass or the first
`DiffResult.field_summary()` call. Without field diff nothing is reread.

## Cleanup

Worker databases are removed immediately after a successful merge or failed
parallel attempt. The complete private workspace, including staged stream
inputs and the final index, is removed on context-manager exit, explicit
`close()`, or a handled failure during construction. Cleanup failures are
emitted as warnings rather than replacing the primary error.

## Sources and compression

Source opening and decompression are delegated to
[`py-jsonl`](https://github.com/rmoralespp/jsonl). `--format json` opens the
same decompressed byte stream with `jsonl.open_stream()` and parses top-level
array elements incrementally with `ijson`. The supported sources and
compression formats are listed in the
[README](../README.md#sources-and-compression).

Zstandard follows `py-jsonl`, which relies on the standard-library zstd support
added in Python 3.14, so `.zst` is unsupported on earlier Python versions.
Compression and HTTP transfer do not affect the reported line numbers: they
are decoded physical lines for JSONL and one-based array-element ordinals for
`--format json`.
