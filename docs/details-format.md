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
  configured JMESPath expression, or `null` when `--where` was not used. With
  `--field-diff`, it also contains `"field_diff": true`.
- **`change`**: one per changed identity. `op` is `added`, `deleted`, or
  `modified`. `old_line` is present for deletions and modifications;
  `new_line` is present for additions and modifications. Equal records are not
  emitted.
- **`summary`**: always last after a successful write, with all four totals.

## Notes

The normal stdout summary is unchanged when `--details` is used, unless
`--quiet` suppresses it. A write failure can leave a partial details file and
returns exit code `2`.

## Field-level changes

`--field-diff` adds a `changes` array to each `modified` event:

```bash
jsonl-diff old.jsonl new.jsonl --key id \
  --details changes.jsonl \
  --field-diff
```

```json
{
  "type": "change",
  "op": "modified",
  "key": [123],
  "old_line": 500,
  "new_line": 721,
  "changes": [
    {"path": "/address/city", "old": "Madrid", "new": "Barcelona"},
    {"path": "/name", "old": "John", "new": "Jonathan"}
  ]
}
```

Paths are RFC 6901 JSON Pointers. Object members are ordered
lexicographically and array items by index. Arrays remain order-sensitive and
are compared positionally. For an added member or trailing array item, `old`
is omitted; for a deleted one, `new` is omitted. This distinguishes a missing
value from JSON `null`.

Ignored fields are excluded from field changes. Added and deleted record
events do not receive a `changes` array.

The initial fingerprint comparison remains unchanged. Only records classified
as modified receive a structural diff. When modifications exist, each source
is read a second time; target metadata and modified records are retained
temporarily in the disk-backed SQLite workspace. Stdin is therefore not
supported with `--field-diff`.
