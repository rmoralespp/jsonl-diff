## Releases

## Unreleased

* **Fixed:** Reject `--details FILE` with exit code `3` when it refers to the same file as OLD or NEW, preventing the 
    report from overwriting an input.
* **Fixed:** Terminate indexing worker processes when parallel indexing is interrupted (e.g. `KeyboardInterrupt`) or a
  worker fails to start, before the temporary workspace is removed, so no workers outlive the comparison. A worker
  start failure is now reported as `could not start indexing workers` (exit code `2`) instead of `output failed`.
* **Changed:** Resolve `DiffResult.changes()` in SQLite so only changed identities reach Python: unfiltered iteration
  scans OLD once with a `LEFT JOIN`, and operation filters run a dedicated query (~1.5x faster unfiltered, ~3x faster
  filtered on 1M records). Order and output are unchanged.
* **Changed:** Speed up `--schema-diff` profiling: classify value types with an exact-class lookup and key fields by
  `(parent, name)`, building JSON Pointers once per distinct field at flush time (~37% less schema overhead on indexing).
* **Added:** Document running summary, schema diff, and details in a single indexing pass and querying the details
  report afterward (README, details format, Python API).
* **Changed:** Measure coverage inside parallel indexing worker processes (`concurrency = ["multiprocessing"]`, `patch = ["subprocess"]`).
* **Added:** Tests for result lifecycle, cleanup warnings, resource errors, stream staging, ignore paths, `--where` errors,
  schema flush boundaries and CLI edge cases, raising coverage from 82% to 98%.

## v0.2.1

* **Changed:** Speed up incremental JSON array parsing: pass top-level JSON array streams directly to ijson.items(), 
  keeping token parsing and item construction in the native yajl2_c pipeline
* **Changed:** Parse top-level JSON arrays through `ijson`'s native `items()` pipeline, avoiding Python event bridging 
  while preserving incremental reads and exact numeric semantics.
* **Changed:** Always index OLD and NEW in parallel worker processes. File-like inputs and stdin are staged in the temporary workspace so workers and error recovery can reopen them.
* **Removed:** Breaking - Remove the `--max-temp` CLI option and `max_temp` Python API parameter, including workspace accounting and SQLite page limits.
* **Changed:** Speed up array number normalization
* **Changed:** Speed up identity extraction with precompiled C-level field lookups while preserving missing-field and null-key behavior.
* **Changed:** Breaking - Make `msgspec` a required dependency and the single JSONL decoder and JSON encoder for identities, content, and details, removing optional fallback behavior and recursive Python canonicalization.
* **Changed:** Breaking - Require Python 3.10+, matching `msgspec 0.21.1`.
* **Changed:** Breaking - Identity and content comparison now preserve parsed numeric representation; for example, `1` and `1.0`, or `1.0` and `1.00`, are different.

## v0.1.5

* **Added:** Allow objects and arrays as identity 
* **Changed:** Use locked uv modes in CI and CD

## v0.1.4

* **Added:** `--missing-key {error,null}` and `missing_key=` control how missing identity fields are 
  handled. `error` (default) preserves the prior `InputError` behavior. `null` treats missing fields 
  like explicit `null`: allowed for composite identities

* **Added:** Top-level JSON array input with `--format json` and
  `format="json"`, parsed incrementally with `ijson` through
  `py-jsonl.open_stream()`. JSONL remains the default format.

## v0.1.3

* **Added:** Optional `msgspec` accelerator (`pip install jsonl-diff[speedups]`, Python 3.10+) for C-based decoding 
    and canonicalization, speeding up large diffs ~2x end-to-end. Falls back to pure Python with identical results,
    except that the accelerator rejects integer literals longer than CPython's ~4300-digit limit.
* **Changed:** Speed up record canonicalization ~4x on large datasets with faster string escaping, serialization, 
   and integral-value hashing. Comparison results and identity/`--details` key notation are unchanged.
* **Changed:** Speed up the disk-backed SQLite index phase with batched inserts, optimized change-summary queries, 
    tuned connection settings (~4x faster reads). Duplicate detection and results are unchanged.
* **Changed:** Duplicate object property names now follow JSON last-wins semantics instead of raising an error, in 
    both `msgspec` and pure-Python parsers.

## v0.1.2

- **Added:** `--schema-diff` and `schema_diff=True` compare disk-backed observed
  field profiles, reporting added/removed fields and changes in types,
  nullability, and requiredness. `--schema-ignore` provides independent RFC
  6901 exclusions for schema profiling.
- **Added:** Tolerated duplicate identities are counted per source and exposed
  as deterministic API and `--details` diagnostics with selected/discarded
  physical lines and canonical-content equality.
- **Changed:** Tolerated duplicates now produce CLI exit code `1`, even when
  the selected OLD and NEW records otherwise compare equal.
- **Docs:** Clarify that composite identities may contain `null` components, but uniqueness is always enforced on the complete identity tuple.
- **Changed:** Internal JSON value types now distinguish parser-produced `Decimal` numbers from native Python `int`/`float` values accepted only by numeric canonicalization helpers.
- **Changed:** Compile `--where` expressions once and reuse them during indexing.
- **Fixed:** Trim whitespace from `--key` names and reject empty values correctly.
- **Fixed:** Links to documentation files referenced from the README file
- **Fixed:** Details JSONL writes ordinary counters, line numbers, keys, and field values without forcing scientific notation.

## v0.1.1

- **Added:** `--where` to filter which records participate in the comparison using a JMESPath expression.
- **Changed:** The tmp SQLite index no longer stores the full canonical content BLOB.
- **Changed:** A `null` value is now accepted for a component of a composite (multi-field) `--key`.
- **Changed:** README synthesized into a quick-start-focused overview; detailed reference material moved to `docs/`.


## v0.1.0

- **Added:** initial commit - MVP
- **Added:** Readme badges
