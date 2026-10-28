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

## Field-diff reread

The index stores fingerprints rather than records, so `field_diff=True` /
`--field-diff` adds a sequential pass after the summary is computed. For each
side, SQLite lists the modified rows in physical-line order; the source is
reread from the start, only those lines are decoded, `--ignore` is applied, and
the canonical body must reproduce the indexed length and SHA-256 digest. A
mismatch, a missing line, or an unreadable source fails the context entry with
`InputError` (`source changed during comparison`) before any result or details
file is produced.

Verified canonical bodies are appended to a `field_bodies` table in physical
order and indexed by side and identity once both sides are stored.
`changes()` reads both sides in identity order alongside the
change query and computes each patch lazily, so patches are never held in
memory together. Every member of a canonical body is itself canonical, so the
patch walker splits objects into undecoded members, compares them as bytes, and
decodes only the values that changed. Extra disk usage is about twice the canonical size of the
modified records. The details writer pre-aggregates `field_summary` totals
for at most one insert batch of distinct paths and merges them into a
`field_counts` table keyed by path, whose primary-key order also yields the
sorted output. Stable schemas never leave memory, while dynamic property names
(for example, objects keyed by ID) spill to disk instead of growing RAM.

The reread decodes only modified lines, but every line still has to be read and
split, and compressed sources are decompressed again; `--format json` sources
are fully reparsed by `ijson`. Staged stdin and file-like inputs are reread
from the workspace copy; URLs are downloaded again. Without `field_diff`
nothing is reread and no extra table is created.

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
| Zstandard (`.zst`) |   Python 3.14+   |          Python 3.14+           |
| ZIP archive        |        No        |               No                |

Zstandard availability follows `py-jsonl` and its use of the standard-library
zstd support added in Python 3.14; it is not supported by this project on earlier
Python versions. Compression and HTTP transfer boundaries do not affect the
reported decoded JSONL line numbers. For `--format json`, the corresponding
locations are one-based array-element ordinals rather than physical lines.
