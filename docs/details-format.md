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
{"added":1,"deleted":1,"equal":0,"modified":1,"new_duplicates":0,"old_duplicates":0,"type":"summary"}
```

## Record types

- **`meta`**: always first. `key` contains normalized identity-field names;
  `ignore` contains the configured ignore pointers; `where` contains the
  configured JMESPath expression, or `null` when `--where` was not used;
  `duplicates` records the duplicate policy once for the whole report.
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
  contributes two.

Duplicate events follow `meta` and precede `change` events:

```jsonl
{"duplicates":"first","ignore":[],"key":["id"],"type":"meta","where":null}
{"content_equal":true,"discarded_line":2,"key":[1],"selected_line":1,"source":"OLD","type":"duplicate"}
{"content_equal":false,"discarded_line":3,"key":[1],"selected_line":1,"source":"OLD","type":"duplicate"}
{"added":0,"deleted":0,"equal":1,"modified":0,"new_duplicates":0,"old_duplicates":2,"type":"summary"}
```

## Notes

The normal stdout summary is unchanged when `--details` is used, unless
`--quiet` suppresses it. A write failure can leave a partial details file and
returns exit code `2`.
