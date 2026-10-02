# Parallel indexing

[← Back to README](../README.md)

How `jsonl-diff` indexes both inputs concurrently, merges their temporary
databases, and preserves useful errors when a worker fails.

## Process lifecycle

The OLD and NEW inputs are independent until comparison starts, so
`DiffResult.__enter__()` always indexes them in two worker processes:

1. The parent creates a private temporary workspace.
2. Each worker parses one input and writes a private `side0.sqlite3` or
   `side1.sqlite3` index.
3. The parent waits for both workers before opening its own SQLite connection,
   so no live connection is inherited across a process boundary.
4. Successful worker databases are attached and copied into the final
   `index.sqlite3` database.
5. The worker databases are detached and removed before summary calculation
   and result iteration begin.

The merge uses `ATTACH DATABASE` and `INSERT INTO ... SELECT ...`. Records are
already keyed by side and identity, so SQLite performs the copy without
re-parsing either input.

The multiprocessing context follows the Python interpreter's default start
method. This is typically `spawn` on Windows and macOS; current Python versions
may use `forkserver` rather than `fork` by default on POSIX.

## Streams and stdin

Worker inputs must be re-openable. Local paths and URLs can be passed directly,
but file-like objects and stdin cannot be reopened reliably by another process.
The parent therefore copies those sources into the private workspace before
starting the workers.

This staging keeps stream support consistent across multiprocessing start
methods and gives the error-recovery pass a complete source to reread. It also
means stream inputs require temporary space for both their encoded content and
their SQLite indexes. These files are removed with the workspace.

## Worker failures

Workers do not serialize exceptions back to the parent. A worker that catches
an error closes its database and exits non-zero without printing a child
traceback. The parent always joins both workers and checks their exit codes.

If either worker failed, neither partial database is merged. The parent creates
a fresh final index and re-indexes OLD followed by NEW in its own process. This
sequential recovery pass reproduces the public exception type, message, source,
line number, and OLD-before-NEW ordering without requiring every exception to
be pickleable.

The same mechanism handles abnormal exits such as an out-of-memory kill. If the
failure was transient and does not recur during recovery, comparison can still
finish successfully. The tradeoff is that an input failing near its end may be
read twice before the error is reported.

## Temporary storage

Parallel indexing temporarily holds both worker databases. During the merge,
the growing final database also coexists with them, so peak workspace usage can
be approximately twice the final index size. Worker databases are deleted as
soon as the merge attempt finishes. The temporary volume must have enough free
space for this peak.

## Application entry points

Start methods other than `fork` import the application's `__main__` module in
each child. Python applications using the `jsonl_diff` API must protect code
that starts a comparison:

```python
from jsonl_diff import diff


def main():
    with diff("old.jsonl", "new.jsonl", key="id") as result:
        print(result.summary)


if __name__ == "__main__":
    main()
```

Without that guard, spawn-based platforms can rerun module-level application
code in each child or raise Python's multiprocessing bootstrap error. The
`python -m jsonl_diff` and installed `jsonl-diff` CLI entry points already use
a safe module guard.
