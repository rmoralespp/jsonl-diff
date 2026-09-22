# Disk-backed architecture and temporary files

[← Back to README](../README.md)

How `jsonl-diff` indexes records, bounds memory, limits temporary disk usage,
and delegates source/compression handling.

## SQLite index

Each record is parsed and validated incrementally, normalized, and inserted
into a private SQLite database under an operating-system temporary directory.
The primary index stores the typed canonical identity, original line, content
length, and SHA-256 digest; it does not retain the full canonical bytes. A
uniqueness constraint detects duplicate identities. SQL joins calculate the
summary, and ordered SQLite cursors drive lazy change iteration.

This architecture bounds memory by the records currently being processed and
database buffers; it does not keep the complete decoded inputs or all changes
in RAM. It does require temporary disk space. The identity, line, length, and
digest index remains until the `DiffResult` is closed, so temporary usage
scales with the number of records rather than the combined input size.

With CLI `--field-diff`, the fingerprint pass remains unchanged. When
modified identities exist, each source is read a second time and the normalized
canonical bytes of modified records are added to their existing SQLite index
rows; all other second-pass records are discarded. Structural comparison then
runs one modified pair at a time, emitting RFC 6901 JSON Pointers. This repeats
input transfer and decompression where applicable. Sources must be readable a
second time, so stdin is not supported in this mode.

## `max_temp` / `--max-temp`

`max_temp` / `--max-temp` accepts a positive byte count and acts as a
best-effort budget for files in the workspace owned by `jsonl-diff`; it is not
a global temporary-storage limit for the whole process. The implementation
raises `ResourceError` (CLI exit `2`) when the workspace is observed above the
configured budget. Choose a limit with room for SQLite pages and index
overhead.

During primary indexing, the filesystem-level check (stat-ing every workspace
file) runs periodically and once after each side finishes rather than after
every record; this keeps large-input indexing fast. With `--field-diff`, it
also runs after each second-pass side.
SQLite's own `max_page_count` (derived from `max_temp`) still rejects oversized
writes to the main database immediately, but it does not account for every
file in the workspace and the filesystem check can observe growth between
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
