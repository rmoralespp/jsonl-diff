# Details JSONL format

[← Back to README](../README.md)

`--details FILE` (CLI) streams a deterministic, machine-readable JSONL report
after both inputs have been completely indexed and validated:

```bash
jsonl-diff old.jsonl new.jsonl --key id --details changes.jsonl
```

For an OLD source containing identities `1` and `2`, and a NEW source
containing changed identity `2` and added identity `3`, the file is:

```jsonl
{"duplicates":"error","ignore":[],"key":["id"],"type":"meta","where":null}
{"key":[1],"old_line":1,"op":"deleted","type":"change"}
{"key":[2],"new_line":1,"old_line":2,"op":"modified","type":"change"}
{"key":[3],"new_line":2,"op":"added","type":"change"}
{"added":1,"deleted":1,"equal":0,"modified":1,"type":"summary"}
```

## Record types

- **`meta`**: always first. `key` contains normalized identity-field names;
  `ignore` contains the configured ignore pointers; `where` contains the
  configured JMESPath expression, or `null` when `--where` was not used.
- **`change`**: one per changed identity. `op` is `added`, `deleted`, or
  `modified`. `old_line` is present for deletions and modifications;
  `new_line` is present for additions and modifications. Equal records are not
  emitted.
- **`summary`**: always last after a successful write, with all four totals.

## Notes

The normal stdout summary is unchanged when `--details` is used, unless
`--quiet` suppresses it. A write failure can leave a partial details file and
returns exit code `2`.

Counters, line numbers, and numeric keys use readable JSON number notation.
Numeric precision is preserved; only magnitudes that would require an
extremely long fixed representation use exponent notation.
