## Releases

## Unreleased

* **Added:** `--field-diff` (requires `--details`) and `diff(..., field_diff=True)` add an RFC 6902 `patch` to modified changes and per-path `field_summary` records (`Change.patch`, `FieldChange`, `FieldOperation`).
* **Changed:** Python API keys return JSON integers as `int` instead of `Decimal`; they compare and hash equal, and details output is unchanged.

## v0.2.3

* **Changed:** Refresh `uv.lock` updating dev/docs dependencies. Runtime dependency constraints are unchanged.
* **Fixed:** Close the input reader when indexing fails mid-stream (e.g. a duplicate key error after the first batch). The open handle kept the staged input locked, so on Windows the temporary workspace could not be removed and `could not remove temporary files` was emitted.

## v0.2.2

* **Added:** Support Python 3.15 in classifiers and CI (lint and coverage).
* **Added:** Document single-pass indexing for summary, schema diff, and details, including querying the details report afterward (README, details format, Python API).
* **Changed:** Require `ijson>=3.6.0`, `py-jsonl==1.5.4`, `jmespath>=1.1.0`, and `msgspec>=0.22.0`. Python 3.15 wheels enable the native `yajl2_c` backend instead of the slower pure-Python fallback.
* **Changed:** Optimize `DiffResult.changes()` with SQLite queries, scanning OLD once for unfiltered iteration and using dedicated queries for operation filters (~1.5x and ~3x faster, respectively, on 1M records). Output and ordering remain unchanged.
* **Changed:** Reduce `--schema-diff` indexing overhead by ~37% with exact-class type lookups, `(parent, name)` field keys, and deferred JSON Pointer construction.
* **Changed:** Measure coverage in parallel indexing workers (`concurrency = ["multiprocessing"]`, `patch = ["subprocess"]`).
* **Fixed:** Reject `--details FILE` with exit code `3` if it matches OLD or NEW, preventing input files from being overwritten.
* **Fixed:** Terminate indexing workers on interruption or startup failure before removing the temporary workspace. Report startup failures as `could not start indexing workers` (exit code `2`) instead of `output failed`.
* **Tests:** Expand coverage from 82% to 98%, adding tests for result lifecycle, cleanup warnings, resource errors, stream staging, ignore paths, `--where` errors, schema flush boundaries, and CLI edge cases.

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
