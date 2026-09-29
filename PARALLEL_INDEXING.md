# Parallel indexing

Branch notes for `perf/parallel-index`. This branch is exploratory and is not
merged. `main` is unchanged.

## What this changes

`DiffResult.__enter__` used to index the two sides one after the other:

```python
self._open()
self._index(self._old, 0)
self._index(self._new, 1)
```

The two sides are independent until the comparison query runs, so they are now
indexed in two worker processes and merged. Measured on a 4-CPU machine, each
file diffed against itself (worst case: both sides fully indexed, every record
equal):

| file | records | main | this branch | change |
| --- | ---: | ---: | ---: | ---: |
| regulations.json.gz | 6 056 | 1.021 s | 0.738 s | -27.7 % |
| nomenclatures.json.gz | 67 331 | 5.991 s | 3.412 s | -43.0 % |
| quota_balance_events.json.gz | 893 874 | 43.278 s | 24.950 s | -42.3 % |
| measures.json.gz | 1 687 928 | 265.142 s | 147.731 s | -44.3 % |

Full method, raw results and the rejected alternatives are in
`data/TIMINGS.md` (untracked).

## How it works

1. The temporary workspace is created **before** any process starts, and the
   parent's SQLite connection is opened **after** the workers exit, so no open
   database handle is ever inherited across `fork`.
2. Two workers each index one side into a private `side{0,1}.sqlite3`, reusing
   `DiffResult._index` unchanged.
3. The parent merges both with `ATTACH DATABASE` and
   `INSERT INTO <table> SELECT * FROM worker<n>.<table>`. Rows arrive already
   keyed by `side` and ordered by the worker's own primary key, so this is a
   C-level append into the merged B-tree. Merging 1.79 M rows costs 1.2 s.

SQLite writes were never the bottleneck — the baseline profile put `executemany`
at about 2 % of the run. The reader is 48-63 % of wall time, and running two of
them at once is what produces the gain.

### Error handling

Workers do not report anything back. A worker that fails calls `os._exit(1)`
without unwinding, so no traceback reaches stderr and nothing is serialised.
The parent checks `process.exitcode`, and if either worker failed it discards
both worker databases and indexes sequentially:

```
join both workers
if both exitcode == 0:   merge and continue
else:                    open a fresh index and run _index(old, 0), _index(new, 1)
```

The retry re-reads the offending file and raises the real exception with the
exact type, message, line number and OLD-before-NEW ordering **by
construction** rather than by reconstruction. It also covers hard crashes: an
OOM-killed or segfaulted worker is just another non-zero exit, and if the cause
was transient the retry simply succeeds.

Failure has to be detected explicitly, because a worker that dies mid-file
leaves a **truncated** database holding every record read before it stopped.
Merging that produces a well-formed but silently wrong diff, with the missing
tail reported as added or deleted rows. Iterating the merged results raises
nothing.

Cost: the failure path re-reads both files from the start, so a diff that fails
near the end of a large file takes roughly twice as long to report.

### Why not `Pool` or `ProcessPoolExecutor`

Measured on this machine with a worker calling `os._exit(137)`:

| API | worker killed |
| --- | --- |
| `multiprocessing.Pool` | **hangs** (timed out at 20 s) |
| `concurrent.futures.ProcessPoolExecutor` | `BrokenProcessPool` in 0.1 s |
| `Process` + `join` + `exitcode` (used here) | detected, sequential retry |

`Pool` never acknowledges the dead worker's task and silently replaces the
worker, so `map()` blocks forever. `ProcessPoolExecutor` detects it, but
propagates ordinary exceptions by pickling them, which would require custom
`__reduce__` methods on `InputError` and `DuplicateKeyError` — their
`__init__` reformats the message, so the default reduction corrupts it.
Neither is a simplification.

### Start method

`fork` when available and the process is single-threaded, otherwise `spawn`.
One diff per process, best of 3:

| dataset | sequential | fork | forkserver | spawn |
| --- | ---: | ---: | ---: | ---: |
| regulations.json.gz | 0.834 | **0.479** | 0.682 | 0.695 |
| nomenclatures.json.gz | 5.777 | 3.412 | 3.392 | **3.320** |

`fork` only wins on small inputs; from ~67 k records up the three are equal, so
`forkserver` was dropped as a needless third path. CPython 3.14 moved the Linux
default off `fork`, and 3.12+ warns when forking a multi-threaded process,
hence the `threading.active_count() == 1` guard.

### Worker database removal

The worker databases are removed as soon as the merge commits. The workspace
would clean them up on `close()` anyway, so this is **only** about peak disk,
not about cleanup or file locking — by that point both workers have exited and
the databases are detached, so nothing still holds them open.

| dataset | worker DBs | merged DB | peak workspace |
| --- | ---: | ---: | ---: |
| nomenclatures.json.gz | 16.0 MB | 15.1 MB | 31.1 MB |
| quota_balance_events.json.gz | 229.1 MB | 212.4 MB | 441.5 MB |

Peak is about 2.08x the merged index. Without the removal the run would hold
both copies for the whole comparison phase. The `contextlib.suppress(OSError)`
around it is defensive only: if the unlink ever fails, the workspace cleanup
still removes the files and the result is unaffected.

## When it falls back to sequential indexing

- `--max-temp` is set. Two private databases grow independently, so the parent
  cannot enforce one combined budget, which is what `--max-temp` promises.
- Either side is not a re-openable path (file objects, stdin `-`).
- `JSONL_DIFF_PARALLEL=0`.
- The platform has no `fork` **and** `__main__` is not the jsonl-diff CLI (see
  below).
- Any worker exits non-zero.

## Windows

Windows only offers `spawn`, and the speedup survives it (5.777 s → 3.320 s on
`nomenclatures`). The hazard is that every start method except `fork`
re-imports `__main__` in each worker:

- An embedding application's module-level code runs **once per worker**.
- An embedding application without an `if __name__ == "__main__":` guard cannot
  start workers at all: the child raises multiprocessing's bootstrap
  `RuntimeError`, exits non-zero, and the parent's sequential retry returns the
  correct result — but a child traceback is printed and two process startups
  are wasted.

So automatic parallel indexing requires either `fork`, or `__main__` being the
jsonl-diff CLI, which is safe to re-import:

- `python -m jsonl_diff` — `__main__.__spec__.name == "jsonl_diff"`
- the generated `jsonl-diff` console script — carries its own module guard

`JSONL_DIFF_PARALLEL` is tri-state:

| value | effect |
| --- | --- |
| unset | automatic (the policy above) |
| `1` | force on, including for library callers on spawn-only platforms |
| `0` | force off |

## What still needs checking on Windows

Everything below was verified on Linux, and the `spawn` path was exercised by
patching `_can_fork()` to return `False`. None of it has run on real Windows.

1. **Correctness.** Diff a file against a modified copy and confirm parallel and
   sequential agree byte for byte, including `--details`:

   ```
   jsonl-diff --format json --key <k> --details p.jsonl old.json new.json > p.out
   set JSONL_DIFF_PARALLEL=0
   jsonl-diff --format json --key <k> --details s.jsonl old.json new.json > s.out
   fc /b p.out s.out
   fc /b p.jsonl s.jsonl
   ```

2. **Temporary file removal.** The worker databases are unlinked after the
   merge purely to halve peak disk; the unlink is wrapped in
   `contextlib.suppress(OSError)`, so a Windows failure would be silent and
   harmless. Confirm that `%TEMP%\jsonl-diff-*` is gone after a run, and that
   disk use drops at the merge rather than staying at the merge-time peak (see
   the table above).

3. **`TemporaryDirectory` cleanup.** If any SQLite handle is still open,
   cleanup raises and is reported through `warnings.warn`. Check that no
   `could not remove temporary files` warning appears.

4. **The CLI detection.** Both entry points must be recognised, otherwise
   Windows silently loses the speedup:

   ```
   python -m jsonl_diff ...
   jsonl-diff ...
   ```

   Verify with `--quiet` timing, or instrument `_parallel_eligible()`.

5. **The unguarded-embedder path.** Run a script that calls `jsonl_diff.diff()`
   at module level with no `if __name__ == "__main__":` guard. Expected: no
   workers are started at all (the gate declines), so no child traceback is
   printed and the result is correct. With `JSONL_DIFF_PARALLEL=1` the gate is
   bypassed and the child traceback **is** expected, followed by a correct
   result from the sequential retry.

6. **A frozen build**, if one is ever produced. `spawn` on Windows requires
   `multiprocessing.freeze_support()` in the frozen entry point. The CLI does
   not call it today.

7. **Timings.** None of the numbers in this document were produced on Windows.
   Process creation is considerably more expensive there, so the small-file
   figures in particular may not hold.

## Verification already done (Linux, Python 3.14.7)

- 150/150 tests pass.
- On a mutated `regulations` pair (1 added, 63 deleted, 113 modified) parallel
  and sequential produce byte-identical stdout, `--details` and
  `--schema-diff --details` output.
- Duplicate key on OLD, duplicate key on NEW, truncated JSON, both sides
  failing at once, and a missing file all produce identical messages and exit
  codes with `JSONL_DIFF_PARALLEL=1` and `=0`, with no child traceback.
- A simulated OOM kill (`os._exit(137)` injected into the NEW worker) yields a
  `Summary` and change list identical to an uninterrupted run.
- With `_can_fork()` forced to `False`, the console script ran the real `spawn`
  path and produced stdout and `--details` identical to sequential, with empty
  stderr.
