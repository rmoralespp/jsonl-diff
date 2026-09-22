# Details JSONL format

[← Back to README](../README.md)

`--details FILE` (CLI) streams a deterministic, machine-readable JSONL report
after both inputs have been completely indexed and validated.

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
{"added":1,"deleted":1,"equal":0,"modified":1,"new_duplicates":0,"old_duplicates":0,"type":"summary"}
```

## Record types

- **`meta`**: always first. `key` contains normalized identity-field names;
  `ignore` contains the configured ignore pointers; `where` contains the
  configured JMESPath expression, or `null` when `--where` was not used;
  `duplicates` records the duplicate policy once for the whole report. With
  `--field-diff`, it also contains `field_diff: true`. With `--schema-diff`,
  it also contains `schema_diff: true` and the normalized `schema_ignore`
  pointers.
- **`schema_change`**: emitted only with `--schema-diff`, ordered by RFC 6901
  path and operation before duplicate and record changes. `op` is
  `field_added`, `field_removed`, `types_changed`, `nullability_changed`, or
  `requiredness_changed`. Available `old` and `new` profiles contain
  `parent_objects`, `present`, `missing`, `nulls`, and non-null `types`
  frequencies.
- **`duplicate`**: one per occurrence discarded by `first` or `last`, ordered
  by source, identity, and discarded line. `source` is `OLD` or `NEW`;
  `selected_line` is the occurrence retained by the policy and
  `discarded_line` is the occurrence excluded from reconciliation.
  `content_equal` reports whether their canonical content is equal after
  applying `--ignore`.
- **`change`**: one per changed identity. `op` is `added`, `deleted`, or
  `modified`. `old_line` is present for deletions and modifications;
  `new_line` is present for additions and modifications. Equal records are not
  emitted.
- **`summary`**: always last after a successful write, with the four
  reconciliation totals plus `old_duplicates` and `new_duplicates`. Duplicate
  totals count additional occurrences, so an identity appearing three times
  contributes two. With schema diff enabled, a nested `schema` object contains
  the five schema-change totals.

Duplicate events follow `meta` and precede `change` events:

```jsonl
{"duplicates":"first","ignore":[],"key":["id"],"type":"meta","where":null}
{"content_equal":true,"discarded_line":2,"key":[1],"selected_line":1,"source":"OLD","type":"duplicate"}
{"content_equal":false,"discarded_line":3,"key":[1],"selected_line":1,"source":"OLD","type":"duplicate"}
{"added":0,"deleted":0,"equal":1,"modified":0,"new_duplicates":0,"old_duplicates":2,"type":"summary"}
```

Schema events precede duplicate and record changes:

```jsonl
{"duplicates":"error","ignore":[],"key":["id"],"schema_diff":true,"schema_ignore":[],"type":"meta","where":null}
{"new":{"missing":0,"nulls":0,"parent_objects":2,"present":2,"types":{"string":2}},"old":{"missing":0,"nulls":0,"parent_objects":2,"present":2,"types":{"integer":2}},"op":"types_changed","path":"/age","type":"schema_change"}
{"new":{"missing":0,"nulls":0,"parent_objects":2,"present":2,"types":{"string":2}},"op":"field_added","path":"/country","type":"schema_change"}
{"added":0,"deleted":0,"equal":0,"modified":2,"new_duplicates":0,"old_duplicates":0,"schema":{"fields_added":1,"fields_removed":0,"nullability_changed":0,"requiredness_changed":0,"types_changed":1},"type":"summary"}
```

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
