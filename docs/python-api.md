# Python API reference

[← Back to README](../README.md)

Full reference for `diff()`, `DiffResult`, result models, and the exception
hierarchy. See the main [README](../README.md) for a quick-start example.

## Comparing sources

```python
from jsonl_diff import ChangeOperation, diff

with diff(
        "old.jsonl.gz",
        "new.jsonl.gz",
        key=("country", "customerId"),
        ignore=("/updated_at",),
        where="country == `\"ES\"`",
        duplicates="error",
        max_temp=2_000_000_000,
) as result:
    print(result.summary)

    for change in result.changes(ChangeOperation.MODIFIED):
        print(change.key, change.old_line, change.new_line)
```

The callable signature is:

```python
from typing import Any, Optional, Sequence, Union

from jsonl_diff import DiffResult, DuplicatePolicy


def diff(
        old: Any,
        new: Any,
        *,
        key: Union[str, Sequence[str]],
        ignore: Sequence[str] = (),
        where: Optional[str] = None,
        duplicates: Union[str, DuplicatePolicy] = DuplicatePolicy.ERROR,
        max_temp: Optional[int] = None,
) -> DiffResult:
    ...
```

`diff()` creates a comparison session without opening either source. Entering
its context fully reads, indexes, and validates both sources before exposing
the result. `DiffResult.changes()` is then a lazy iterator over the disk-backed
result rather than a list held in memory.

Field-level expansion is currently a CLI report feature enabled by
`--details FILE --field-diff`; the public Python API exposes record-level
`Change` objects.

Using `DiffResult` as a context manager is required. It owns the temporary
resources and removes its private workspace on exit. A result cannot be entered
more than once. Its summary becomes available after entering and remains
available after closing; iterating changes requires the context to remain open.

## Result models

`result.summary` is an immutable `Summary`:

```python
Summary(equal=10, added=2, deleted=1, modified=3)
```

It exposes integer fields `equal`, `added`, `deleted`, and `modified`, plus the
boolean property `different`. The same values are available directly as
read-only `DiffResult` properties.

Each item from `result.changes()` is an immutable `Change` with:

- `operation`: a `ChangeOperation` enum value (`ADDED`, `DELETED`, or `MODIFIED`);
- `key`: a typed tuple in normalized identity-field order;
- `old` and `new`: optional `SourceLocation` values;
- `old_line` and `new_line`: convenience properties returning a one-based
  physical line or `None`.

For additions, `old`/`old_line` are `None`; for deletions, `new`/`new_line` are
`None`; modifications have both locations. Location sources are labeled `OLD`
and `NEW`.

Pass a `ChangeOperation` to filter without materializing all changes:

```python
with diff("old.jsonl", "new.jsonl", key="id") as result:
    added = result.changes(ChangeOperation.ADDED)
    for change in added:
        print(change.key, change.new_line)
```

With numeric keys, API key components are `decimal.Decimal` values. For
example, JSON identity `7` is returned as `Decimal("7")`; JSON strings and
booleans retain their types. Details JSONL writes numeric keys as JSON numbers,
including arbitrary-precision integers.

## API errors

The public error hierarchy starts with `JsonlDiffError`:

- `ConfigurationError`: invalid keys, ignore pointers, or `max_temp`;
- `InputError`: an invalid source or record; exposes `source` and optional `line`;
- `DuplicateKeyError`: an `InputError` with `key` and the first/repeated physical lines in `lines`;
- `ResourceError`: the temporary index cannot be created, written, or kept within its configured limit.

Failures during `diff()` clean up the workspace before the exception is
raised.
