# Disk-backed architecture and temporary files

[← Back to README](../README.md)

How `jsonl-diff` indexes records, bounds memory, limits temporary disk usage,
and delegates source/compression handling.

## SQLite index

Each record is parsed and validated incrementally, normalized, and inserted
into a private SQLite database under an operating-system temporary directory.
The database stores the typed canonical identity, original line, content
length, and SHA-256 digest; the full canonical bytes are not persisted. A
uniqueness constraint detects duplicate identities. When `first` or `last`
tolerates a collision, a second table stores the discarded occurrence's
identity, physical line, canonical length, and digest. SQL joins calculate the
summary, and ordered SQLite cursors drive lazy change and duplicate iteration.

This architecture bounds memory by the records currently being processed and
database buffers; it does not keep the complete decoded inputs or all changes
in RAM. It does require temporary disk space. The identity, line, length, and
digest index remains until the `DiffResult` is closed, so temporary usage
scales with the number of selected records plus tolerated duplicate
occurrences rather than the combined input size. Duplicate diagnostics compare
stored fingerprints and do not require retaining or rereading full records.

## `max_temp` / `--max-temp`

`max_temp` / `--max-temp` accepts a positive byte count and acts as a
best-effort budget for files in the workspace owned by `jsonl-diff`; it is not
a global temporary-storage limit for the whole process. The implementation
raises `ResourceError` (CLI exit `2`) when the workspace is observed above the
configured budget. Choose a limit with room for SQLite pages and index
overhead.

The filesystem-level check (stat-ing every workspace file) runs every 1024
inserted records per side, plus once more after each side finishes, rather
than after every record; this keeps large-input indexing fast. SQLite's own
`max_page_count` (derived from `max_temp`) still rejects oversized writes to
the main index immediately, but it does not account for every file in the
workspace and the periodic filesystem check can observe growth between
checks.

`py-jsonl` may create its own temporary staging files for remote or compressed
sources. Those files follow `py-jsonl`'s resource policy and are not counted by
`jsonl-diff`'s `max_temp` limit. Total system temporary usage can therefore
exceed the configured value.

## Cleanup

The private workspace is removed on context-manager exit, explicit `close()`,
or a handled failure during construction. Cleanup failures are emitted as
warnings rather than replacing the primary error.

## Sources and compression

Source opening and decompression are delegated to
[`py-jsonl`](https://github.com/rmoralespp/jsonl).

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
reported decoded JSONL line numbers.
