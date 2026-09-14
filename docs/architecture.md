# Disk-backed architecture and temporary files

[← Back to README](../README.md)

How `jsonl-diff` indexes records, bounds memory, limits temporary disk usage,
and delegates source/compression handling.

## SQLite index

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

## `max_temp` / `--max-temp`

`max_temp` / `--max-temp` accepts a positive byte count. The implementation
limits and checks files in the workspace owned by `jsonl-diff`, raising
`ResourceError` (CLI exit `2`) when the index cannot stay within that budget.
Choose a limit with room for SQLite pages and index overhead.

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
