# Details JSONL format

[← Back to README](../README.md)

`--details FILE` (CLI) streams a deterministic, machine-readable JSONL report
after both inputs have been completely indexed and validated.

```bash
jsonl-diff old.jsonl new.jsonl --key id --details changes.jsonl
```

`FILE` must not refer to the same file as OLD or NEW, including aliases such as
relative paths, symlinks, or hard links. Such a target is rejected with exit
code `3` before any input is read, leaving both inputs untouched.

For an OLD source containing identities `1` and `2`, and a NEW source
containing changed identity `2` and added identity `3`, the file is:

```jsonl
{"duplicates":"error","ignore":[],"key":["id"],"missing_key":"error","type":"meta","where":null}
{"key":[1],"old_line":1,"op":"deleted","type":"change"}
{"key":[2],"new_line":1,"old_line":2,"op":"modified","type":"change"}
{"key":[3],"new_line":2,"op":"added","type":"change"}
{"added":1,"deleted":1,"equal":0,"modified":1,"new_duplicates":0,"old_duplicates":0,"type":"summary"}
```

## Record types

- **`meta`**: always first. `key` contains normalized identity-field names;
  `ignore` contains the configured ignore pointers; `where` contains the
  configured JMESPath expression, or `null` when `--where` was not used;
  `duplicates` records the duplicate policy once for the whole report;
  `missing_key` records how absent identity fields are handled. With
  `--schema-diff`, the record also contains `schema_diff: true` and the
  normalized `schema_ignore` pointers. With `--field-diff`, it contains
  `field_diff: true`.
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
  emitted. `key` contains the identity values as JSON values, including
  objects and arrays; object member order is canonicalized and array order is
  preserved. Numeric keys retain their parsed representation and use
  `msgspec` JSON notation, including uppercase `E` for exponents. With
  `--field-diff`, modified changes also contain a `patch` (see
  [Field-level patches](#field-level-patches)).
- **`field_summary`**: emitted only with `--field-diff`, one per changed
  field path, ordered by path, after `change` records and before `summary`.
  `added`, `removed`, and `replaced` count the patch operations on that path.
- **`summary`**: always last after a successful write, with the four
  reconciliation totals plus `old_duplicates` and `new_duplicates`. Duplicate
  totals count additional occurrences, so an identity appearing three times
  contributes two. With schema diff enabled, a nested `schema` object contains
  the five schema-change totals.

Duplicate events follow `meta` and precede `change` events:

```jsonl
{"duplicates":"first","ignore":[],"key":["id"],"missing_key":"error","type":"meta","where":null}
{"content_equal":true,"discarded_line":2,"key":[1],"selected_line":1,"source":"OLD","type":"duplicate"}
{"content_equal":false,"discarded_line":3,"key":[1],"selected_line":1,"source":"OLD","type":"duplicate"}
{"added":0,"deleted":0,"equal":1,"modified":0,"new_duplicates":0,"old_duplicates":2,"type":"summary"}
```

Schema events precede duplicate and record changes:

```jsonl
{"duplicates":"error","ignore":[],"key":["id"],"missing_key":"error","schema_diff":true,"schema_ignore":[],"type":"meta","where":null}
{"new":{"missing":0,"nulls":0,"parent_objects":2,"present":2,"types":{"string":2}},"old":{"missing":0,"nulls":0,"parent_objects":2,"present":2,"types":{"integer":2}},"op":"types_changed","path":"/age","type":"schema_change"}
{"new":{"missing":0,"nulls":0,"parent_objects":2,"present":2,"types":{"string":2}},"op":"field_added","path":"/country","type":"schema_change"}
{"added":0,"deleted":0,"equal":0,"modified":2,"new_duplicates":0,"old_duplicates":0,"schema":{"fields_added":1,"fields_removed":0,"nullability_changed":0,"requiredness_changed":0,"types_changed":1},"type":"summary"}
```

## Field-level patches

`--field-diff` (requires `--details`) adds an [RFC 6902](https://www.rfc-editor.org/rfc/rfc6902)
JSON Patch to every modified change, describing how the OLD record becomes the
NEW one:

```bash
jsonl-diff old.jsonl new.jsonl --key id --details changes.jsonl --field-diff
```

```jsonl
{"duplicates":"error","field_diff":true,"ignore":[],"key":["id"],"missing_key":"error","type":"meta","where":null}
{"key":[1],"new_line":1,"old_line":1,"op":"modified","patch":[{"old":30,"op":"replace","path":"/age","value":31},{"old":"x","op":"remove","path":"/legacy"},{"op":"add","path":"/tags","value":["a"]}],"type":"change"}
{"added":0,"path":"/age","removed":0,"replaced":1,"type":"field_summary"}
{"added":0,"path":"/legacy","removed":1,"replaced":0,"type":"field_summary"}
{"added":1,"path":"/tags","removed":0,"replaced":0,"type":"field_summary"}
{"added":0,"deleted":0,"equal":0,"modified":1,"new_duplicates":0,"old_duplicates":0,"type":"summary"}
```

- Operations are `add`, `remove`, and `replace`, in depth-first member-name
  order within each patch. `path` is an RFC 6901 pointer (`~` → `~0`, `/` → `~1`).
- `value` is the NEW value (`add`, `replace`); `old` is the OLD value
  (`remove`, `replace`). RFC 6902 requires appliers to ignore unknown members,
  so patches remain directly applicable while carrying the before image.
- Objects are compared recursively; member order is irrelevant.
- Arrays are atomic: any difference replaces the whole array with both full
  images. Element moves, insertions, and deletions are not computed.
- Comparison is representation-sensitive, like record comparison: `1` → `1.0`,
  `1.0` → `1.00`, and `1` → `true` are all `replace` operations.
- Fields excluded with `--ignore` never appear, and identity fields never
  change, so a modified record always has a non-empty patch.
- `field_summary` has one record per distinct changed path, so dynamic
  property names produce many records. Totals are aggregated on disk, so
  memory stays bounded.

Patches are computed by rereading only the modified lines of both inputs after
indexing (see [architecture](architecture.md#field-diff-reread)). If an input
changed between the two passes, the run fails with exit code `2` before the
details file is written.

```bash
# Changes of one field across all modified records
jq -c 'select(.type == "change" and .patch) | {key, patch: [.patch[] | select(.path == "/age")]}' changes.jsonl

# Most frequently changed fields
jq -s -c 'map(select(.type == "field_summary")) | sort_by(-(.added + .removed + .replaced)) | .[:10]' changes.jsonl
```

## Querying a report

Every invocation indexes both inputs, and indexing dominates the cost of a
comparison. Since a details file already contains the summary, schema changes
(with `--schema-diff`), duplicates, and record changes, one run can replace
several:

```bash
jsonl-diff old.jsonl new.jsonl --key id --schema-diff --details report.jsonl --quiet
```

The report is plain JSONL, so any line-oriented tool can query it without
re-indexing the inputs. For example, with `jq`:

```bash
# Summary totals, including the nested schema totals
jq -c 'select(.type == "summary")' report.jsonl

# Observed-schema changes
jq -c 'select(.type == "schema_change")' report.jsonl

# Identities of modified records
jq -c 'select(.type == "change" and .op == "modified") | .key' report.jsonl

# Number of added records
jq -s 'map(select(.type == "change" and .op == "added")) | length' report.jsonl
```

Options that change which records are compared or how they are compared
(`--key`, `--ignore`, `--where`, `--duplicates`, `--missing-key`,
`--schema-ignore`, `--format`, `--field-diff`) produce a different report and require a new
run; the `meta` record states the configuration a report was produced with.

## Notes

The normal stdout summary is unchanged when `--details` is used, unless
`--quiet` suppresses it. A write failure can leave a partial details file and
returns exit code `2`.
