"""Strict, disk-backed reconciliation of JSON Lines datasets."""

import argparse
import contextlib
import hashlib
import os
import sqlite3
import sys
import tempfile
import warnings
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from operator import itemgetter
from typing import Any, Callable, Dict, Iterable, Iterator, Optional, Sequence, Tuple, Union

import ijson
import jmespath
import jsonl
import msgspec

utf8 = "utf-8"

JsonIdentityValue = Union[str, Decimal, bool, int, None, list, dict]
IdentityKey = Tuple[JsonIdentityValue, ...]


_MISSING = object()

_SCHEMA_FLUSH_INTERVAL = 1024
# Records are inserted in batches to amortize per-statement overhead.
_INSERT_BATCH = 2048
_SCHEMA_TYPE_INDEXES = {
    "boolean": 3,
    "integer": 4,
    "number": 5,
    "string": 6,
    "object": 7,
    "array": 8,
}
# Exact-class lookup for the common parsed types; Decimal, float and subclasses
# fall back to `_schema_type`, which also decides integer versus number.
_SCHEMA_TYPE_INDEX_BY_CLASS = {
    bool: _SCHEMA_TYPE_INDEXES["boolean"],
    int: _SCHEMA_TYPE_INDEXES["integer"],
    str: _SCHEMA_TYPE_INDEXES["string"],
    dict: _SCHEMA_TYPE_INDEXES["object"],
    list: _SCHEMA_TYPE_INDEXES["array"],
}

_CANONICAL_ENCODER = msgspec.json.Encoder(decimal_format="number", order="sorted")
_INPUT_DECODER = msgspec.json.Decoder(float_hook=Decimal)
# Field patches split canonical objects into members kept as undecoded bytes.
_RAW_MEMBERS_DECODER = msgspec.json.Decoder(Dict[str, msgspec.Raw])
_OBJECT_START = ord("{")


class JsonlDiffError(Exception):
    """Base exception for jsonl-diff failures."""


class ConfigurationError(JsonlDiffError, ValueError):
    """Raised when comparison options are invalid."""


class InputError(JsonlDiffError):
    """Raised when an input source or record is invalid."""

    def __init__(self, message: str, source: str, line: Optional[int] = None):
        self.source = source
        self.line = line
        location = " in {}".format(source)
        if line is not None:
            location += " at line {}".format(line)
        super().__init__("{}{}".format(message, location))


class DuplicateKeyError(InputError):
    """Raised when an identity occurs more than once in one source."""

    def __init__(self, key: IdentityKey, source: str, lines: Sequence[int]):
        self.key = key
        self.lines = tuple(lines)

        message = "duplicate key {} on lines {}".format(
            _canonical_text(list(key)),
            ", ".join(str(line) for line in lines),
        )
        super().__init__(message, source)


class ResourceError(JsonlDiffError):
    """Raised when a resource limit prevents comparison."""


class ChangeOperation(str, Enum):
    """A changed record classification."""

    ADDED = "added"
    DELETED = "deleted"
    MODIFIED = "modified"


class DuplicatePolicy(str, Enum):
    """How duplicate identities are handled."""

    ERROR = "error"
    FIRST = "first"
    LAST = "last"


class MissingKeyPolicy(str, Enum):
    """How an absent identity field is handled."""

    ERROR = "error"
    NULL = "null"


class SchemaChangeOperation(str, Enum):
    """An observed schema change classification."""

    FIELD_ADDED = "field_added"
    FIELD_REMOVED = "field_removed"
    TYPES_CHANGED = "types_changed"
    NULLABILITY_CHANGED = "nullability_changed"
    REQUIREDNESS_CHANGED = "requiredness_changed"


class FieldOperation(str, Enum):
    """An RFC 6902 operation kind used by field-level patches."""

    ADD = "add"
    REMOVE = "remove"
    REPLACE = "replace"


@dataclass(frozen=True)
class SourceLocation:
    """A physical source location."""

    source: str
    line: int


@dataclass(frozen=True)
class SchemaFieldProfile:
    """Observed statistics for one object field."""

    path: str
    parent_objects: int
    present: int
    nulls: int
    boolean_count: int
    integer_count: int
    number_count: int
    string_count: int
    object_count: int
    array_count: int

    @property
    def missing(self) -> int:
        """Return the number of parent objects without this field."""
        return self.parent_objects - self.present

    @property
    def nullable(self) -> bool:
        """Return whether an explicit null was observed."""
        return bool(self.nulls)

    @property
    def required(self) -> bool:
        """Return whether the field was present in every observed parent object."""
        return self.present == self.parent_objects

    @property
    def type_counts(self) -> Dict[str, int]:
        """Return observed non-null JSON types and their frequencies."""
        counts = (
            ("boolean", self.boolean_count),
            ("integer", self.integer_count),
            ("number", self.number_count),
            ("string", self.string_count),
            ("object", self.object_count),
            ("array", self.array_count),
        )
        return {name: count for name, count in counts if count}

    @property
    def types(self) -> Tuple[str, ...]:
        """Return observed non-null JSON types in stable order."""
        return tuple(self.type_counts)


@dataclass(frozen=True)
class SchemaChange:
    """One observed field, type, nullability, or requiredness change."""

    operation: SchemaChangeOperation
    path: str
    old: Optional[SchemaFieldProfile]
    new: Optional[SchemaFieldProfile]


@dataclass(frozen=True)
class SchemaSummary:
    """Observed schema change totals."""

    fields_added: int = 0
    fields_removed: int = 0
    types_changed: int = 0
    nullability_changed: int = 0
    requiredness_changed: int = 0

    @property
    def different(self) -> bool:
        """Return whether any observed schema changes were found."""
        return any((
            self.fields_added,
            self.fields_removed,
            self.types_changed,
            self.nullability_changed,
            self.requiredness_changed,
        ))


@dataclass(frozen=True)
class Summary:
    """Comparison totals."""

    equal: int
    added: int
    deleted: int
    modified: int
    old_duplicates: int = 0
    new_duplicates: int = 0
    schema: Optional[SchemaSummary] = None

    @property
    def different(self) -> bool:
        """Return whether any changed records were found."""
        return bool(self.added or self.deleted or self.modified)

    @property
    def has_duplicates(self) -> bool:
        """Return whether either source contained duplicate identities."""
        return bool(self.old_duplicates or self.new_duplicates)

    @property
    def has_schema_changes(self) -> bool:
        """Return whether observed schema changes were found."""
        return self.schema is not None and self.schema.different

    @property
    def has_issues(self) -> bool:
        """Return whether record, duplicate, or observed schema issues were found."""
        return self.different or self.has_duplicates or self.has_schema_changes


@dataclass(frozen=True)
class DiffConfig:
    """Normalized comparison configuration."""

    key: Tuple[str, ...]
    ignore: Tuple[str, ...] = ()
    where: Optional[str] = None
    duplicates: DuplicatePolicy = DuplicatePolicy.ERROR
    where_expression: Any = None
    schema_diff: bool = False
    schema_ignore: Tuple[str, ...] = ()
    format: str = "jsonl"
    missing_key: MissingKeyPolicy = MissingKeyPolicy.ERROR
    field_diff: bool = False


@dataclass(frozen=True)
class FieldChange:
    """
    One RFC 6902 operation between the OLD and NEW versions of a modified record.

    `op` is a `FieldOperation`, and `path` is an RFC 6901 pointer. `old` is
    meaningful for `remove` and `replace`; `value` for `add` and `replace`.
    """

    op: FieldOperation
    path: str
    old: Any = None
    value: Any = None


@dataclass(frozen=True)
class FieldSummary:
    """Field-level operation totals for one RFC 6901 path across modified records."""

    path: str
    added: int
    removed: int
    replaced: int


@dataclass(frozen=True)
class Change:
    """One added, deleted, or modified identity."""

    operation: ChangeOperation
    key: IdentityKey
    old: Optional[SourceLocation]
    new: Optional[SourceLocation]
    # Populated for modified identities only when `field_diff` is enabled.
    patch: Optional[Tuple[FieldChange, ...]] = None

    @property
    def old_line(self) -> Optional[int]:
        """Return the OLD physical line, when present."""
        return None if self.old is None else self.old.line

    @property
    def new_line(self) -> Optional[int]:
        """Return the NEW physical line, when present."""
        return None if self.new is None else self.new.line


@dataclass(frozen=True)
class Duplicate:
    """One occurrence discarded by a tolerant duplicate policy."""

    key: IdentityKey
    selected: SourceLocation
    discarded: SourceLocation
    content_equal: bool

    @property
    def source(self) -> str:
        """Return the source containing the duplicate identity."""
        return self.selected.source

    @property
    def selected_line(self) -> int:
        """Return the physical line selected by the duplicate policy."""
        return self.selected.line

    @property
    def discarded_line(self) -> int:
        """Return the physical line discarded by the duplicate policy."""
        return self.discarded.line


def _canonical(value: Any) -> bytes:
    return _CANONICAL_ENCODER.encode(value)


def _canonical_text(value: Any) -> str:
    return _canonical(value).decode(utf8)


def _fingerprint(canonical: bytes) -> Tuple[int, bytes]:
    return len(canonical), hashlib.sha256(canonical).digest()


def _pointer_child(path: str, token: str) -> str:
    # Most member names need no escaping, so skip the replacements for them.
    if "~" in token or "/" in token:
        token = token.replace("~", "~0").replace("/", "~1")
    return path + "/" + token


def _field_keys(old: Dict[str, Any], new: Dict[str, Any]) -> Iterator[str]:
    old_keys = old.keys()
    # Bodies are decoded from canonical bytes, so equal key sets are already sorted.
    return iter(old if old_keys == new.keys() else sorted(old_keys | new.keys()))


def _field_patch(
    old: bytes,
    new: bytes,
    decode: Callable[[Any], Any] = _INPUT_DECODER.decode,
    members: Callable[[Any], Dict[str, msgspec.Raw]] = _RAW_MEMBERS_DECODER.decode,
) -> list:
    """
    Return the RFC 6902 operations turning canonical object `old` into `new`.

    Objects recurse; arrays and scalars are replaced atomically. Every member
    of a canonical encoding is itself canonical, so members are compared as
    raw bytes: equality is representation-sensitive like record comparison
    (`1`, `1.0`, `1.00`, and `true` all differ), and only changed values are
    decoded. Nested objects are traversed with an explicit stack, depth
    first, so any depth accepted while indexing is also accepted here.
    """
    patch = []
    old_members, new_members = members(old), members(new)
    stack = [("", old_members, new_members, _field_keys(old_members, new_members))]
    while stack:
        path, old_object, new_object, keys = stack[-1]
        for key in keys:
            before = old_object.get(key)
            after = new_object.get(key)
            if before == after:
                continue
            if before is None:
                patch.append(FieldChange(FieldOperation.ADD, _pointer_child(path, key), value=decode(after)))
            elif after is None:
                patch.append(FieldChange(FieldOperation.REMOVE, _pointer_child(path, key), old=decode(before)))
            elif memoryview(before)[0] == _OBJECT_START and memoryview(after)[0] == _OBJECT_START:
                before, after = members(before), members(after)
                stack.append((_pointer_child(path, key), before, after, _field_keys(before, after)))
                break
            else:
                child = _pointer_child(path, key)
                patch.append(FieldChange(FieldOperation.REPLACE, child, decode(before), decode(after)))
        else:
            stack.pop()
    return patch


def _schema_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    elif isinstance(value, str):
        return "string"
    elif isinstance(value, dict):
        return "object"
    elif isinstance(value, list):
        return "array"
    elif isinstance(value, Decimal):
        return "integer" if value == value.to_integral_value() else "number"
    elif isinstance(value, int):
        return "integer"
    elif isinstance(value, float):
        return "integer" if value.is_integer() else "number"
    else:
        raise ValueError("unsupported JSON value type: {}".format(type(value).__name__))


def _profile_schema(
    record: Dict[str, Any],
    ignore_tree: Dict[str, Any],
    fields: Dict[Tuple[str, str], Any],
    objects: Dict[str, int],
):
    """
    Accumulate observed field counts for one record.

    Fields are keyed by ``(parent_path, name)`` so JSON Pointers are built once
    per distinct field at flush time instead of once per occurrence.
    """
    object_index = _SCHEMA_TYPE_INDEXES["object"]
    stack = [("", record, ignore_tree)]
    while stack:
        path, value, tree = stack.pop()
        objects[path] = objects.get(path, 0) + 1
        for name, item in value.items():
            child_tree = tree.get(name) if tree else None
            if child_tree is not None:
                if _MISSING in child_tree:
                    continue
                if isinstance(item, list):
                    raise ValueError("schema ignore paths may not traverse arrays")
            counts = fields.get((path, name))
            if counts is None:
                counts = [path, 0, 0, 0, 0, 0, 0, 0, 0]
                fields[(path, name)] = counts
            counts[1] += 1
            if item is None:
                counts[2] += 1
                continue
            index = _SCHEMA_TYPE_INDEX_BY_CLASS.get(type(item))
            if index is None:
                index = _SCHEMA_TYPE_INDEXES[_schema_type(item)]
            counts[index] += 1
            if index == object_index:
                stack.append((_pointer_child(path, name), item, child_tree or {}))


def _parse_pointer(pointer: str) -> Tuple[str, ...]:
    if not pointer.startswith("/") or pointer == "/":
        if pointer == "/":
            return ("",)
        raise ConfigurationError("ignore paths must be non-empty RFC 6901 JSON Pointers")

    tokens = []
    for token in pointer[1:].split("/"):
        index = 0
        decoded = []
        while index < len(token):
            if token[index] != "~":
                decoded.append(token[index])
                index += 1
            elif index + 1 >= len(token) or token[index + 1] not in "01":
                raise ConfigurationError("invalid JSON Pointer escape in {!r}".format(pointer))
            else:
                decoded.append("/" if token[index + 1] == "1" else "~")
                index += 2
        tokens.append("".join(decoded))
    return tuple(tokens)


def _ignore_tree(paths: Sequence[str], keys: Sequence[str]) -> Dict[str, Any]:
    tree = dict()
    for pointer in paths:
        tokens = _parse_pointer(pointer)
        if len(tokens) == 1 and tokens[0] in keys:
            raise ConfigurationError("identity field {!r} cannot be ignored".format(tokens[0]))
        node = tree
        for token in tokens:
            node = node.setdefault(token, {})
        node[_MISSING] = True
    return tree


def _remove_ignored(value: Any, tree: Dict[str, Any]) -> Any:
    if not tree:
        return value
    if isinstance(value, list):
        raise ValueError("ignore paths may not traverse arrays")
    if not isinstance(value, dict):
        return value
    result = {}
    for name, item in value.items():
        child = tree.get(name)
        if child is None:
            result[name] = item
        elif _MISSING not in child:
            result[name] = _remove_ignored(item, child)
    return result


def _identity(
    record: Dict[str, Any],
    keys: Sequence[str],
    missing_key: MissingKeyPolicy = MissingKeyPolicy.ERROR,
) -> IdentityKey:
    composite = len(keys) > 1
    values = []
    for name in keys:
        if name not in record:
            if missing_key != MissingKeyPolicy.NULL:
                raise ValueError("missing identity field {!r}".format(name))
            value = None
        else:
            value = record[name]

        if value is None:
            # Composite identities may contain null components; uniqueness is
            # still enforced on the complete identity tuple. A single-field
            # identity may not be null, since that would collapse every null
            # record into one indistinguishable identity.
            if not composite:
                raise ValueError("identity field {!r} must be a non-null scalar".format(name))
        elif not isinstance(value, (str, Decimal, bool, int, list, dict)):
            raise ValueError("identity field {!r} must be a JSON value".format(name))

        values.append(value)
    return tuple(values)


def _compile_where(expression: str) -> Any:
    try:
        return jmespath.compile(expression)
    except jmespath.exceptions.JMESPathError as error:
        raise ConfigurationError("invalid --where expression {!r}: {}".format(expression, error)) from error


_ADDED_QUERY = """
    SELECT n.identity, NULL, n.line
    FROM records AS n
    WHERE n.side = 1 AND NOT EXISTS (
        SELECT 1 FROM records AS o WHERE o.side = 0 AND o.identity = n.identity
    )
"""
_DELETED_QUERY = """
    SELECT o.identity, o.line, NULL
    FROM records AS o
    WHERE o.side = 0 AND NOT EXISTS (
        SELECT 1 FROM records AS n WHERE n.side = 1 AND n.identity = o.identity
    )
"""
_MODIFIED_QUERY = """
    SELECT o.identity, o.line, n.line
    FROM records AS o
    JOIN records AS n
      ON o.side = 0 AND n.side = 1 AND o.identity = n.identity
    WHERE o.length <> n.length OR o.digest <> n.digest
"""
# Unfiltered iteration resolves deleted and modified identities in a single
# pass over OLD instead of scanning it once per operation.
_DELETED_OR_MODIFIED_QUERY = """
    SELECT o.identity, o.line, n.line
    FROM records AS o
    LEFT JOIN records AS n
      ON n.side = 1 AND n.identity = o.identity
    WHERE o.side = 0 AND (n.identity IS NULL OR o.length <> n.length OR o.digest <> n.digest)
"""
# Each query yields (identity, old_line, new_line); ordering by the canonical
# identity BLOB (bytewise) keeps iteration deterministic.
_CHANGE_QUERIES = {
    None: _DELETED_OR_MODIFIED_QUERY + " UNION ALL " + _ADDED_QUERY + " ORDER BY 1",
    ChangeOperation.ADDED: _ADDED_QUERY + " ORDER BY 1",
    ChangeOperation.DELETED: _DELETED_QUERY + " ORDER BY 1",
    ChangeOperation.MODIFIED: _MODIFIED_QUERY + " ORDER BY 1",
}
# Modified identities with both fingerprints, resolved with a single identity
# join; each side's reread then sorts only these rows by physical line.
_FIELD_LINES_QUERY = """
    CREATE TABLE field_lines AS
    SELECT o.identity, o.line AS old_line, o.length AS old_length, o.digest AS old_digest,
           n.line AS new_line, n.length AS new_length, n.digest AS new_digest
    FROM records AS o
    JOIN records AS n
      ON o.side = 0 AND n.side = 1 AND o.identity = n.identity
    WHERE o.length <> n.length OR o.digest <> n.digest
"""
_SIDE_LINES_QUERIES = (
    "SELECT old_line, identity, old_length, old_digest FROM field_lines ORDER BY old_line",
    "SELECT new_line, identity, new_length, new_digest FROM field_lines ORDER BY new_line",
)
# Both sides hold exactly the modified identities, so reading them in identity
# order pairs them with the modified rows of every `_CHANGE_QUERIES` entry.
_FIELD_BODIES_QUERY = "SELECT identity, body FROM field_bodies WHERE side = ? ORDER BY identity"
_FIELD_BODIES_INSERT = "INSERT INTO field_bodies VALUES (?, ?, ?)"
# `field_counts` column per patch operation, in `field_summary` order.
_FIELD_COUNT_COLUMNS = {
    FieldOperation.ADD: 0,
    FieldOperation.REMOVE: 1,
    FieldOperation.REPLACE: 2,
}
_FIELD_COUNTS_UPSERT = """
    INSERT INTO field_counts VALUES (?, ?, ?, ?)
    ON CONFLICT (path) DO UPDATE SET
        added = added + excluded.added,
        removed = removed + excluded.removed,
        replaced = replaced + excluded.replaced
"""
# TEXT keys compare as UTF-8 bytes, which matches Python's code-point order.
_FIELD_COUNTS_QUERY = "SELECT path, added, removed, replaced FROM field_counts ORDER BY path"
_SIDE_NAMES = ("OLD", "NEW")
# Exceptions a reader raises for malformed or unreadable input.
_READ_ERRORS = (OSError, EOFError, ValueError, RuntimeError, ijson.JSONError)


@contextlib.contextmanager
def _sqlite_resource(message: str) -> Iterator[None]:
    try:
        yield
    except sqlite3.DatabaseError as error:
        raise ResourceError(message) from error


def _passthrough(line: str) -> str:
    return line


class _FieldCounter:
    """
    Per-path `field_summary` totals with bounded memory.

    Totals are pre-aggregated for at most `_INSERT_BATCH` distinct paths and
    then merged into SQLite, so stable schemas never touch the database while
    dynamic member names (objects keyed by ID) spill to disk instead of RAM.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._pending = {}
        with _sqlite_resource("could not write the temporary index"):
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS field_counts (
                    path TEXT PRIMARY KEY,
                    added INTEGER NOT NULL,
                    removed INTEGER NOT NULL,
                    replaced INTEGER NOT NULL
                ) WITHOUT ROWID
                """,
            )
            # DELETE, unlike DROP, is allowed while other cursors are reading.
            connection.execute("DELETE FROM field_counts")

    def add(self, patch: Iterable[FieldChange]) -> None:
        pending = self._pending
        for field_change in patch:
            counts = pending.get(field_change.path)
            if counts is None:
                if len(pending) >= _INSERT_BATCH:
                    self.flush()
                counts = pending[field_change.path] = [0, 0, 0]
            counts[_FIELD_COUNT_COLUMNS[field_change.op]] += 1

    def flush(self) -> None:
        if self._pending:
            with _sqlite_resource("could not write the temporary index"):
                self._connection.executemany(
                    _FIELD_COUNTS_UPSERT,
                    [(path, *counts) for path, counts in self._pending.items()],
                )
            self._pending.clear()


class _TopLevelArrayReader:
    """Validate the root value without interrupting ijson's native items pipeline."""

    __slots__ = ("_stream", "_validated")

    def __init__(self, stream: Any):
        self._stream = stream
        self._validated = False

    def read(self, size: int = -1) -> Any:
        data = self._stream.read(size)
        if not self._validated and data:
            # ijson probes streams with read(0), and leading JSON whitespace may
            # span chunks, so wait until the first non-whitespace token arrives.
            whitespace = " \t\r\n" if isinstance(data, str) else b" \t\r\n"
            valid_non_array = '{"-0123456789tfn' if isinstance(data, str) else b'{"-0123456789tfn'
            token = data.lstrip(whitespace)
            if token:
                # Reject valid non-array roots with the existing error; unknown
                # starters pass through so ijson preserves its detailed syntax errors.
                if token[0] in valid_non_array:
                    raise ValueError("expected a top-level JSON array")
                self._validated = True
        # Validation only inspects data; the native parser receives the original chunk.
        return data


def _array_records(source: Any) -> Iterator[Any]:
    with jsonl.open_stream(source) as stream:
        # Passing the stream directly keeps parsing and item construction in yajl2_c;
        # feeding parse() events into items() would rebuild every record in Python.
        yield from ijson.items(_TopLevelArrayReader(stream), "item", use_float=False)


def _records(
    source: Any,
    on_error: Any,
    input_format: str,
    decode: Callable[[str], Any] = _INPUT_DECODER.decode,
) -> Iterator[Any]:
    """Iterate source records; `decode` applies to JSONL lines only, arrays arrive decoded."""
    if input_format == "json":
        yield from _array_records(source)
    else:
        # Non-integer numbers become Decimal while integers stay native `int`, preserving their parsed representation.
        # Duplicate keys use last-wins.
        yield from jsonl.load(source, cls=decode, _on_error=on_error)


def _multiprocessing_context() -> Any:
    import multiprocessing

    # Defer to the interpreter's default start method instead of forcing one.
    # That is `spawn` on Windows and macOS, and CPython 3.14 is migrating POSIX
    # off `fork`; following the default keeps behaviour aligned with the
    # platform and clear of the fork-in-a-thread deprecation. Every default
    # start method pickles the config and, except for `fork`, re-imports the
    # interpreter per worker -- startup cost that is noise next to indexing on
    # any real input.
    return multiprocessing.get_context()


def _index_worker(db_path: str, source: str, side: int, config: "DiffConfig") -> None:
    """
    Index one side into a private database, or exit non-zero on failure.

    Nothing is reported back: the parent detects failure from the exit status
    and re-raises by re-indexing sequentially, which reproduces the original
    exception exactly rather than shipping a copy of it across processes.
    """
    result = None
    try:
        result = DiffResult(source, source, config)
        result._index_into(db_path, source, side)
    except BaseException:
        if result is not None:
            result.close()
        # Exit without unwinding so the traceback never reaches stderr; the
        # parent raises the real error itself.
        os._exit(1)
    result.close()


def _start_worker(context: Any, db_path: str, source: str, side: int, config: "DiffConfig") -> Any:
    process = context.Process(target=_index_worker, args=(db_path, source, side, config), daemon=True)
    try:
        process.start()
    except OSError as error:
        raise ResourceError("could not start indexing workers") from error
    return process


def _join_workers(workers: Sequence[Any]) -> bool:
    """Wait for every worker and report whether any of them failed."""
    failed = False
    for process in workers:
        process.join()
        failed = failed or process.exitcode != 0
    return failed


def _stop_workers(workers: Sequence[Any], timeout: float = 5.0) -> None:
    for process in workers:
        if process.is_alive():
            process.terminate()
    for process in workers:
        process.join(timeout)
        if process.is_alive():
            process.kill()
            process.join()


class DiffResult:
    """A context-managed, disk-backed comparison result."""

    def __init__(self, old: Any, new: Any, config: DiffConfig):
        self._old = old
        self._new = new
        self.config = config
        self._ignore_tree = _ignore_tree(config.ignore, config.key)
        self._schema_ignore_tree = _ignore_tree(config.schema_ignore, ())
        self._identity_getter = itemgetter(*config.key)
        self._identity_is_composite = len(config.key) > 1
        self._where = config.where_expression  # Compiled during configuration validation and reused for every record.
        self._workspace = None
        self._connection = None
        self._summary_value = None
        self._field_counts_ready = False
        self._field_counter = None
        self._sources = None
        self._started = False

    def _open_workspace(self) -> None:
        self._workspace = tempfile.TemporaryDirectory(prefix="jsonl-diff-")

    def _open_connection(self, path: str) -> None:
        self._connection = sqlite3.connect(path)
        self._configure_database()
        self._create_schema()

    def _configure_database(self):
        self._connection.execute("PRAGMA journal_mode = OFF")
        self._connection.execute("PRAGMA synchronous = OFF")
        self._connection.execute("PRAGMA temp_store = FILE")
        self._connection.execute("PRAGMA cache_size = -65536")

    def _create_schema(self):
        self._connection.execute(
            """
            CREATE TABLE records (
                side INTEGER NOT NULL,
                identity BLOB NOT NULL,
                line INTEGER NOT NULL,
                length INTEGER NOT NULL,
                digest BLOB NOT NULL,
                PRIMARY KEY (side, identity)
            ) WITHOUT ROWID
            """,
        )
        if self.config.schema_diff:
            self._connection.execute(
                """
                CREATE TABLE schema_fields (
                    side INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    parent_path TEXT NOT NULL,
                    present INTEGER NOT NULL,
                    nulls INTEGER NOT NULL,
                    booleans INTEGER NOT NULL,
                    integers INTEGER NOT NULL,
                    numbers INTEGER NOT NULL,
                    strings INTEGER NOT NULL,
                    objects INTEGER NOT NULL,
                    arrays INTEGER NOT NULL,
                    PRIMARY KEY (side, path)
                ) WITHOUT ROWID
                """,
            )
            self._connection.execute(
                """
                CREATE TABLE schema_objects (
                    side INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    occurrences INTEGER NOT NULL,
                    PRIMARY KEY (side, path)
                ) WITHOUT ROWID
                """,
            )
        self._connection.execute(
            """
            CREATE TABLE duplicate_records (
                side INTEGER NOT NULL,
                identity BLOB NOT NULL,
                line INTEGER NOT NULL,
                length INTEGER NOT NULL,
                digest BLOB NOT NULL,
                PRIMARY KEY (side, identity, line)
            ) WITHOUT ROWID
            """,
        )

    def _require_summary(self) -> Summary:
        if self._summary_value is None:
            raise RuntimeError("diff() must be used as a context manager")
        return self._summary_value

    @property
    def summary(self) -> Summary:
        """Return comparison totals after the context has been entered."""
        return self._require_summary()

    @property
    def equal(self) -> int:
        return self._require_summary().equal

    @property
    def added(self) -> int:
        return self._require_summary().added

    @property
    def deleted(self) -> int:
        return self._require_summary().deleted

    @property
    def modified(self) -> int:
        return self._require_summary().modified

    @property
    def old_duplicates(self) -> int:
        return self._require_summary().old_duplicates

    @property
    def new_duplicates(self) -> int:
        return self._require_summary().new_duplicates

    @property
    def different(self) -> bool:
        return self._require_summary().different

    @property
    def has_duplicates(self) -> bool:
        return self._require_summary().has_duplicates

    @property
    def schema_summary(self) -> Optional[SchemaSummary]:
        return self._require_summary().schema

    @property
    def has_schema_changes(self) -> bool:
        return self._require_summary().has_schema_changes

    @property
    def has_issues(self) -> bool:
        return self._require_summary().has_issues

    def changes(self, operation: Optional[ChangeOperation] = None) -> Iterator[Change]:
        """Iterate changed identities in deterministic order."""
        if self._connection is None:
            raise RuntimeError("the diff result is closed")
        requested = None if operation is None else ChangeOperation(operation)
        # SQLite resolves identity matching over the primary key, so only
        # changed rows reach Python.
        rows = self._connection.execute(_CHANGE_QUERIES[requested])
        bodies = None
        if self.config.field_diff and requested in (None, ChangeOperation.MODIFIED):
            bodies = zip(
                self._connection.execute(_FIELD_BODIES_QUERY, (0,)),
                self._connection.execute(_FIELD_BODIES_QUERY, (1,)),
            )
        for identity, old_line, new_line in rows:
            patch = None
            if old_line is None:
                change_operation = ChangeOperation.ADDED
            elif new_line is None:
                change_operation = ChangeOperation.DELETED
            else:
                change_operation = ChangeOperation.MODIFIED
                if bodies is not None:
                    patch = self._patch(identity, next(bodies, None))
            yield self._change(change_operation, identity, old_line, new_line, patch)

    @staticmethod
    def _patch(identity: bytes, bodies: Any) -> Tuple[FieldChange, ...]:
        if bodies is None or bodies[0][0] != identity or bodies[1][0] != identity:
            raise RuntimeError("field bodies are out of step with modified identities")
        return tuple(_field_patch(bodies[0][1], bodies[1][1]))

    def field_summary(self) -> Iterator[FieldSummary]:
        """Iterate per-path field operation totals across modified records, ordered by path."""
        if self._connection is None:
            raise RuntimeError("the diff result is closed")
        elif not self.config.field_diff:
            raise RuntimeError("field diff was not enabled")

        if not self._field_counts_ready:
            for _ in self._changes_with_field_counts(ChangeOperation.MODIFIED):
                pass
        with _sqlite_resource("could not read the temporary index"):
            for path, added, removed, replaced in self._connection.execute(_FIELD_COUNTS_QUERY):
                yield FieldSummary(path, added, removed, replaced)

    def _changes_with_field_counts(self, operation: Optional[ChangeOperation] = None) -> Iterator[Change]:
        """Iterate `changes()` while building `field_counts` once, if field diff needs it."""
        counter = None
        if self._connection is None:
            raise RuntimeError("the diff result is closed")
        elif self.config.field_diff and not self._field_counts_ready:
            # Creating a counter resets the table, so it invalidates any
            # suspended builder; only the active counter may write or finish.
            counter = self._field_counter = _FieldCounter(self._connection)
        for change in self.changes(operation):
            if counter is not None and change.patch is not None:
                if counter is self._field_counter:
                    counter.add(change.patch)
                else:
                    counter = None
            yield change
        if counter is not None and counter is self._field_counter:
            counter.flush()
            self._field_counts_ready = True

    def schema_changes(
        self,
        operation: Optional[SchemaChangeOperation] = None,
    ) -> Iterator[SchemaChange]:
        """Iterate observed schema changes in deterministic order."""

        if self._connection is None:
            raise RuntimeError("the diff result is closed")
        elif not self.config.schema_diff:
            raise RuntimeError("schema diff was not enabled")

        requested = None if operation is None else SchemaChangeOperation(operation)
        old_profiles = iter(self._schema_profiles(0))
        new_profiles = iter(self._schema_profiles(1))
        old_profile = next(old_profiles, None)
        new_profile = next(new_profiles, None)

        while old_profile is not None or new_profile is not None:
            comparison = (
                1
                if old_profile is None
                else -1
                if new_profile is None
                else (old_profile.path > new_profile.path) - (old_profile.path < new_profile.path)
            )
            if comparison < 0:
                changes = (
                    SchemaChange(
                        SchemaChangeOperation.FIELD_REMOVED,
                        old_profile.path,
                        old_profile,
                        None,
                    ),
                )
                old_profile = next(old_profiles, None)
            elif comparison > 0:
                changes = (
                    SchemaChange(
                        SchemaChangeOperation.FIELD_ADDED,
                        new_profile.path,
                        None,
                        new_profile,
                    ),
                )
                new_profile = next(new_profiles, None)
            else:
                changes = tuple(self._changed_schema_dimensions(old_profile, new_profile))
                old_profile = next(old_profiles, None)
                new_profile = next(new_profiles, None)

            for change in changes:
                if requested is None or change.operation == requested:
                    yield change

    def _schema_profiles(self, side: int) -> Iterator[SchemaFieldProfile]:
        rows = self._connection.execute(
            """
            SELECT
                f.path,
                p.occurrences,
                f.present,
                f.nulls,
                f.booleans,
                f.integers,
                f.numbers,
                f.strings,
                f.objects,
                f.arrays
            FROM schema_fields AS f
            JOIN schema_objects AS p
              ON p.side = f.side AND p.path = f.parent_path
            WHERE f.side = ?
            ORDER BY f.path
            """,
            (side,),
        )
        for row in rows:
            yield SchemaFieldProfile(row[0], *(int(value) for value in row[1:]))

    @staticmethod
    def _changed_schema_dimensions(old: SchemaFieldProfile, new: SchemaFieldProfile) -> Iterator[SchemaChange]:
        if old.types != new.types:
            yield SchemaChange(SchemaChangeOperation.TYPES_CHANGED, old.path, old, new)
        if old.nullable != new.nullable:
            yield SchemaChange(SchemaChangeOperation.NULLABILITY_CHANGED, old.path, old, new)
        if old.required != new.required:
            yield SchemaChange(SchemaChangeOperation.REQUIREDNESS_CHANGED, old.path, old, new)

    def duplicates(self) -> Iterator[Duplicate]:
        """Iterate discarded duplicate occurrences in deterministic order."""
        if self._connection is None:
            raise RuntimeError("the diff result is closed")
        rows = self._connection.execute(
            """
            SELECT
                d.side,
                d.identity,
                r.line,
                d.line,
                d.length = r.length AND d.digest = r.digest
            FROM duplicate_records AS d
            JOIN records AS r USING (side, identity)
            ORDER BY d.side, d.identity, d.line
            """,
        )
        for side, identity, selected_line, discarded_line, content_equal in rows:
            source = _SIDE_NAMES[side]
            key = tuple(_INPUT_DECODER.decode(identity))
            yield Duplicate(
                key,
                SourceLocation(source, selected_line),
                SourceLocation(source, discarded_line),
                bool(content_equal),
            )

    @staticmethod
    def _change(
        operation: ChangeOperation,
        identity: bytes,
        old_line: Optional[int],
        new_line: Optional[int],
        patch: Optional[Tuple[FieldChange, ...]] = None,
    ) -> Change:
        key = tuple(_INPUT_DECODER.decode(identity))
        return Change(
            operation,
            key,
            None if old_line is None else SourceLocation("OLD", old_line),
            None if new_line is None else SourceLocation("NEW", new_line),
            patch,
        )

    def close(self):
        """Release database and temporary files."""
        if self._connection is not None:
            try:
                self._connection.close()
            except sqlite3.Error as error:
                warnings.warn(
                    "could not close temporary database: {}".format(error),
                    stacklevel=2,
                )
            self._connection = None
        if self._workspace is not None:
            try:
                self._workspace.cleanup()
            except OSError as error:
                warnings.warn(
                    "could not remove temporary files: {}".format(error),
                    stacklevel=2,
                )
            self._workspace = None

    def __enter__(self) -> "DiffResult":
        if self._started:
            raise RuntimeError("the diff result cannot be entered more than once")
        self._started = True
        try:
            self._index_parallel()
            self._summary_value = self._calculate_summary()
            if self.config.field_diff:
                self._store_field_bodies()
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any):
        self.close()

    def _insert_tolerated_record(
        self,
        cursor: sqlite3.Cursor,
        values: Tuple[Any, ...],
    ):
        cursor.execute("INSERT OR IGNORE INTO records VALUES (?, ?, ?, ?, ?)", values)
        if cursor.rowcount != 0:
            return
        side, identity, line, length, digest = values
        if self.config.duplicates == DuplicatePolicy.FIRST:
            cursor.execute("INSERT INTO duplicate_records VALUES (?, ?, ?, ?, ?)", values)
            return
        cursor.execute(
            """
            INSERT INTO duplicate_records
            SELECT side, identity, line, length, digest
            FROM records
            WHERE side = ? AND identity = ?
            """,
            (side, identity),
        )
        cursor.execute(
            """
            UPDATE records
            SET line = ?, length = ?, digest = ?
            WHERE side = ? AND identity = ?
            """,
            (line, length, digest, side, identity),
        )

    def _extract_identity(self, record: Dict[str, Any]) -> IdentityKey:
        """Extract a typed identity with a C-level lookup and exact fallbacks."""
        try:
            value = self._identity_getter(record)
        except KeyError:
            return _identity(record, self.config.key, self.config.missing_key)
        # Parsed records contain only supported JSON types. Composite keys allow
        # null components, so a successful lookup is already fully validated.
        if self._identity_is_composite:
            return value
        if value is None:
            return _identity(record, self.config.key, self.config.missing_key)
        return (value,)

    def _insert_records(self, records: Iterable[Any], side: int, source: str):
        cursor = self._connection.cursor()
        schema_fields = {}
        schema_objects = {}
        pending = []
        for line, record in enumerate(records, start=1):
            if not isinstance(record, dict):
                raise InputError("each record must be a JSON object", source, line)
            if self._where is not None:
                try:
                    matched = bool(self._where.search(record))
                except (TypeError, ValueError) as error:
                    raise InputError(str(error), source, line) from error
                if not matched:
                    if self.config.schema_diff and line % _SCHEMA_FLUSH_INTERVAL == 0:
                        if pending:
                            self._flush_record_batch(cursor, pending, source)
                        self._flush_schema_profile(
                            cursor,
                            side,
                            schema_fields,
                            schema_objects,
                        )
                    continue
            try:
                key = self._extract_identity(record)
                normalized = _remove_ignored(record, self._ignore_tree)
                canonical = _canonical(normalized)
                identity = _canonical(list(key))
                length, digest = _fingerprint(canonical)
            except (TypeError, ValueError) as error:
                raise InputError(str(error), source, line) from error
            if self.config.schema_diff:
                try:
                    _profile_schema(
                        record,
                        self._schema_ignore_tree,
                        schema_fields,
                        schema_objects,
                    )
                except (TypeError, ValueError) as error:
                    raise InputError(str(error), source, line) from error
            values = (side, identity, line, length, digest)
            if self.config.duplicates == DuplicatePolicy.ERROR:
                pending.append(values)
                if len(pending) >= _INSERT_BATCH:
                    self._flush_record_batch(cursor, pending, source)
            else:
                try:
                    self._insert_tolerated_record(cursor, values)
                except sqlite3.DatabaseError as error:
                    raise ResourceError("could not write the temporary index") from error
            if self.config.schema_diff and line % _SCHEMA_FLUSH_INTERVAL == 0:
                if pending:
                    self._flush_record_batch(cursor, pending, source)
                self._flush_schema_profile(cursor, side, schema_fields, schema_objects)
        if pending:
            self._flush_record_batch(cursor, pending, source)
        if self.config.schema_diff:
            self._flush_schema_profile(cursor, side, schema_fields, schema_objects)

    @staticmethod
    def _flush_record_batch(
        cursor: sqlite3.Cursor,
        pending: list,
        source: str,
    ):
        try:
            cursor.executemany("INSERT INTO records VALUES (?, ?, ?, ?, ?)", pending)
        except sqlite3.IntegrityError:
            # A duplicate identity exists within this batch or against a
            # previously inserted row. Undo this batch's partial inserts (each
            # row has a unique physical line, so this can never remove an earlier
            # row) and replay row by row to report the exact colliding identity
            # and its first physical line, matching the per-record behavior.
            cursor.executemany(
                "DELETE FROM records WHERE side = ? AND identity = ? AND line = ?",
                [(row[0], row[1], row[2]) for row in pending],
            )
            for row in pending:
                try:
                    cursor.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?)", row)
                except sqlite3.IntegrityError as error:
                    first = cursor.execute(
                        "SELECT line FROM records WHERE side = ? AND identity = ?",
                        (row[0], row[1]),
                    ).fetchone()[0]
                    key = tuple(_INPUT_DECODER.decode(row[1]))
                    raise DuplicateKeyError(key, source, (first, row[2])) from error
            pending.clear()
        except sqlite3.DatabaseError as error:
            raise ResourceError("could not write the temporary index") from error
        else:
            pending.clear()

    @staticmethod
    def _flush_schema_profile(
        cursor: sqlite3.Cursor,
        side: int,
        fields: Dict[Tuple[str, str], Any],
        objects: Dict[str, int],
    ):
        if objects:
            cursor.executemany(
                """
                INSERT INTO schema_objects VALUES (?, ?, ?)
                ON CONFLICT(side, path) DO UPDATE SET
                    occurrences = schema_objects.occurrences + excluded.occurrences
                """,
                ((side, path, count) for path, count in objects.items()),
            )
            objects.clear()
        if fields:
            cursor.executemany(
                """
                INSERT INTO schema_fields VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(side, path) DO UPDATE SET
                    present = schema_fields.present + excluded.present,
                    nulls = schema_fields.nulls + excluded.nulls,
                    booleans = schema_fields.booleans + excluded.booleans,
                    integers = schema_fields.integers + excluded.integers,
                    numbers = schema_fields.numbers + excluded.numbers,
                    strings = schema_fields.strings + excluded.strings,
                    objects = schema_fields.objects + excluded.objects,
                    arrays = schema_fields.arrays + excluded.arrays
                """,
                (
                    (side, _pointer_child(parent, name), parent, *counts[1:])
                    for (parent, name), counts in fields.items()
                ),
            )
            fields.clear()

    def _index_into(self, db_path: str, source: Any, side: int) -> None:
        """Index one side into the private database at `db_path` (worker entry point)."""
        self._open_connection(db_path)
        self._index(source, side)
        self._connection.commit()

    def _index(self, source: Any, side: int):
        name = _SIDE_NAMES[side]
        self._read_source(
            lambda on_error: _records(source, on_error, self.config.format),
            lambda records: self._insert_records(records, side, name),
            name,
            "could not build the temporary index",
        )

    def _read_source(
        self,
        read: Callable[[Callable[[int, Exception], None]], Iterator[Any]],
        consume: Callable[[Iterator[Any]], None],
        name: str,
        sqlite_message: str,
    ) -> None:
        """Consume a source in one transaction, mapping failures to public errors."""
        error_line = [None]

        def on_error(line: int, error: Exception):
            error_line[0] = line

        try:
            # Close the reader even when a batch fails mid-stream: a suspended
            # generator keeps the input open, and Windows cannot then remove it.
            with self._connection, contextlib.closing(read(on_error)) as items:
                consume(items)
        except JsonlDiffError:
            raise
        except _READ_ERRORS as error:
            message = "invalid input ({})".format(type(error).__name__)
            raise InputError(message, name, error_line[0]) from error
        except sqlite3.DatabaseError as error:
            raise ResourceError(sqlite_message) from error

    def _index_parallel(self) -> None:
        context = _multiprocessing_context()
        try:
            self._open_workspace()
        except OSError as error:
            raise ResourceError("could not create the temporary index") from error
        paths = [
            os.path.join(self._workspace.name, "side{}.sqlite3".format(side))
            for side in (0, 1)
        ]
        sources = [
            self._parallel_source(source, side)
            for side, source in enumerate((self._old, self._new))
        ]
        # Staged paths stay reopenable after indexing for the field-diff reread.
        self._sources = sources
        # `fork` inherits the compiled `--where` expression and every other
        # start method pickles it; either way the worker gets the config as it
        # stands here.
        workers = []
        try:
            for side, source in enumerate(sources):
                workers.append(_start_worker(context, paths[side], source, side, self.config))
            failed = _join_workers(workers)
        except BaseException:
            # Stop workers before the caller removes the workspace they write to.
            _stop_workers(workers)
            raise

        try:
            self._open_connection(os.path.join(self._workspace.name, "index.sqlite3"))
            if not failed:
                self._merge_worker_databases(paths)
        except (OSError, sqlite3.Error) as error:
            raise ResourceError("could not build the temporary index") from error
        finally:
            for path in paths:
                # Halve the peak workspace: the merged index is about the same
                # size as the two worker databases together, and the rest of
                # the run would otherwise hold both. Purely a disk-space
                # measure -- the workers have exited and the databases are
                # already detached, so nothing still holds these files open,
                # and `close()` would remove them regardless.
                with contextlib.suppress(OSError):
                    os.remove(path)

        if failed:
            # A worker exited non-zero, so its database holds only the records
            # read before it stopped; merging that would yield a well-formed
            # but silently truncated diff. Index sequentially instead: the same
            # input raises the same error here, with the exact message, line
            # and OLD-before-NEW ordering the sequential path always produced.
            # A failure that does not reproduce was transient, and this pass
            # simply succeeds.
            self._index(sources[0], 0)
            self._index(sources[1], 1)

    def _parallel_source(self, source: Any, side: int) -> str:
        if isinstance(source, (str, os.PathLike)):
            return os.fspath(source)

        path = os.path.join(self._workspace.name, "input{}".format(side))
        name = _SIDE_NAMES[side]
        try:
            with open(path, "wb") as destination:
                self._copy_parallel_source(source, destination, name)
        except OSError as error:
            raise ResourceError("could not write the temporary input") from error
        return path

    def _copy_parallel_source(self, source: Any, destination: Any, name: str) -> None:
        """
        Stage a stream so spawned workers and error recovery can reopen it.

        Stdin and arbitrary file-like objects are not reliably pickleable,
        reopenable, or seekable. Copying them in bounded chunks gives workers
        a path and preserves a complete source for the sequential recovery pass
        without loading the input into memory.
        """
        while True:
            try:
                chunk = source.read(1024 * 1024)
            except (OSError, EOFError, ValueError, RuntimeError) as error:
                raise InputError(
                    "invalid input ({})".format(type(error).__name__),
                    name,
                ) from error
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode(utf8)
            try:
                destination.write(chunk)
            except OSError as error:
                raise ResourceError("could not write the temporary input") from error

    def _merge_worker_databases(self, paths: Sequence[str]) -> None:
        tables = ["records", "duplicate_records"]
        if self.config.schema_diff:
            tables += ["schema_fields", "schema_objects"]
        for index, path in enumerate(paths):
            alias = "worker{}".format(index)
            # ATTACH and DETACH cannot run inside an open transaction, so each
            # worker database is merged in its own committed transaction.
            self._connection.execute(
                "ATTACH DATABASE ? AS {}".format(alias),
                (path,),
            )
            try:
                with self._connection:
                    for table in tables:
                        # Rows arrive already keyed by side and sorted by the
                        # worker's own primary key, so this is a C-level
                        # append into the merged B-tree.
                        self._connection.execute(
                            "INSERT INTO {0} SELECT * FROM {1}.{0}".format(table, alias),
                        )
            finally:
                self._connection.execute("DETACH DATABASE {}".format(alias))

    def _calculate_summary(self) -> Summary:
        # Derive the four record totals from a single identity join (matched
        # pairs, plus how many of them are content-equal) and two per-side range
        # counts, instead of running the join twice and two anti-joins. Every
        # access uses the (side, identity) primary key, and the arithmetic below
        # recovers added/deleted/modified without extra scans.
        row = self._connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM records WHERE side = 0),
                (SELECT COUNT(*) FROM records WHERE side = 1),
                matches.matched,
                matches.equal,
                (SELECT COUNT(*) FROM duplicate_records WHERE side = 0),
                (SELECT COUNT(*) FROM duplicate_records WHERE side = 1)
            FROM (
                SELECT
                    COUNT(*) AS matched,
                    COALESCE(
                        SUM(o.length = n.length AND o.digest = n.digest), 0
                    ) AS equal
                FROM records AS o
                JOIN records AS n
                  ON o.side = 0 AND n.side = 1 AND o.identity = n.identity
            ) AS matches
            """,
        ).fetchone()
        total_old, total_new, matched, equal, old_dups, new_dups = (int(value or 0) for value in row)
        added = total_new - matched
        deleted = total_old - matched
        modified = matched - equal
        schema = self._calculate_schema_summary() if self.config.schema_diff else None
        return Summary(
            equal,
            added,
            deleted,
            modified,
            old_dups,
            new_dups,
            schema=schema,
        )

    def _calculate_schema_summary(self) -> SchemaSummary:
        counts = dict.fromkeys(SchemaChangeOperation, 0)
        for change in self.schema_changes():
            counts[change.operation] += 1
        return SchemaSummary(
            fields_added=counts[SchemaChangeOperation.FIELD_ADDED],
            fields_removed=counts[SchemaChangeOperation.FIELD_REMOVED],
            types_changed=counts[SchemaChangeOperation.TYPES_CHANGED],
            nullability_changed=counts[SchemaChangeOperation.NULLABILITY_CHANGED],
            requiredness_changed=counts[SchemaChangeOperation.REQUIREDNESS_CHANGED],
        )

    def _store_field_bodies(self) -> None:
        """
        Keep the canonical OLD and NEW bodies of every modified identity.

        The index holds only fingerprints, so field patches need the contents
        again. Each source is reread sequentially and only the modified lines
        are decoded. Every body must reproduce its indexed fingerprint: a
        source that changed between passes fails here, before any output.
        """
        message = "could not write the temporary index"
        with _sqlite_resource(message):
            # A rowid table appends bodies in physical-line order; one index
            # built afterward serves the identity-ordered reads. Large bodies
            # inserted out of key order into a WITHOUT ROWID table cost
            # ~1.6x more in total, growing with the share of modified records.
            self._connection.execute(
                """
                CREATE TABLE field_bodies (
                    side INTEGER NOT NULL,
                    identity BLOB NOT NULL,
                    body BLOB NOT NULL
                )
                """,
            )
        if not self._summary_value.modified:
            return
        with _sqlite_resource(message):
            self._connection.execute(_FIELD_LINES_QUERY)
        for side, source in enumerate(self._sources):
            self._store_side_bodies(source, side)
        with _sqlite_resource(message), self._connection:
            self._connection.execute("DROP TABLE field_lines")
            self._connection.execute("CREATE UNIQUE INDEX field_bodies_identity ON field_bodies (side, identity)")

    def _store_side_bodies(self, source: str, side: int) -> None:
        name = _SIDE_NAMES[side]
        input_format = self.config.format
        # Undecoded JSONL lines keep the reread cheap: only modified lines are
        # parsed. Top-level array items already arrive decoded.
        parse = _INPUT_DECODER.decode if input_format == "jsonl" else None
        self._read_source(
            lambda on_error: _records(source, on_error, input_format, decode=_passthrough),
            lambda items: self._insert_field_bodies(items, side, name, parse),
            name,
            "could not write the temporary index",
        )

    def _insert_field_bodies(
        self,
        items: Iterable[Any],
        side: int,
        name: str,
        parse: Optional[Callable[[str], Any]],
    ) -> None:
        wanted = self._connection.execute(_SIDE_LINES_QUERIES[side])
        cursor = self._connection.cursor()
        pending = []
        target = next(wanted)
        for line, item in enumerate(items, start=1):
            if line != target[0]:
                continue
            try:
                record = item if parse is None else parse(item)
                canonical = _canonical(_remove_ignored(record, self._ignore_tree))
            except (TypeError, ValueError):
                canonical = None
            if canonical is None or _fingerprint(canonical) != (target[2], target[3]):
                raise InputError("source changed during comparison", name, line)
            pending.append((side, target[1], canonical))
            if len(pending) >= _INSERT_BATCH:
                cursor.executemany(_FIELD_BODIES_INSERT, pending)
                pending.clear()
            target = next(wanted, None)
            if target is None:
                break
        if target is not None:
            raise InputError("source changed during comparison", name)
        cursor.executemany(_FIELD_BODIES_INSERT, pending)


def _configuration(
    key: Union[str, Sequence[str]],
    ignore: Sequence[str],
    where: Optional[str],
    duplicates: Union[str, DuplicatePolicy],
    schema_diff: bool,
    schema_ignore: Sequence[str],
    input_format: str,
    missing_key: Union[str, MissingKeyPolicy] = MissingKeyPolicy.ERROR,
    field_diff: bool = False,
) -> DiffConfig:
    keys = (key,) if isinstance(key, str) else tuple(key)
    if not keys or any(not isinstance(name, str) or not name for name in keys):
        raise ConfigurationError("at least one non-empty identity field is required")
    if len(set(keys)) != len(keys):
        raise ConfigurationError("identity fields must not be repeated")
    keys = tuple(sorted(keys))
    ignores = tuple(dict.fromkeys(ignore))
    _ignore_tree(ignores, keys)
    schema_ignores = tuple(dict.fromkeys(schema_ignore))
    _ignore_tree(schema_ignores, ())
    if schema_ignores and not schema_diff:
        raise ConfigurationError("schema_ignore requires schema_diff=True")
    where_expression = None if where is None else _compile_where(where)

    try:
        duplicate_policy = DuplicatePolicy(duplicates)
    except ValueError as error:
        raise ConfigurationError("invalid duplicate policy {!r}".format(duplicates)) from error

    try:
        missing_key_policy = MissingKeyPolicy(missing_key)
    except ValueError as error:
        raise ConfigurationError("invalid missing-key policy {!r}".format(missing_key)) from error

    if input_format not in ("jsonl", "json"):
        raise ConfigurationError("invalid format {!r}".format(input_format))

    return DiffConfig(
        key=keys,
        ignore=ignores,
        where=where,
        duplicates=duplicate_policy,
        missing_key=missing_key_policy,
        where_expression=where_expression,
        schema_diff=schema_diff,
        schema_ignore=schema_ignores,
        format=input_format,
        field_diff=bool(field_diff),
    )


def diff(
    old: Any,
    new: Any,
    *,
    key: Union[str, Sequence[str]],
    ignore: Sequence[str] = (),
    where: Optional[str] = None,
    duplicates: Union[str, DuplicatePolicy] = DuplicatePolicy.ERROR,
    missing_key: Union[str, MissingKeyPolicy] = MissingKeyPolicy.ERROR,
    schema_diff: bool = False,
    schema_ignore: Sequence[str] = (),
    format: str = "jsonl",  # noqa: A002
    field_diff: bool = False,
) -> DiffResult:
    """Create a context-managed, disk-backed comparison."""
    config = _configuration(
        key,
        ignore,
        where,
        duplicates,
        schema_diff,
        schema_ignore,
        format,
        missing_key,
        field_diff,
    )
    return DiffResult(old, new, config)


def _field_change_dict(change: FieldChange) -> Dict[str, Any]:
    op = FieldOperation(change.op)
    result = {"op": op.value, "path": change.path}
    if op is not FieldOperation.ADD:
        result["old"] = change.old
    if op is not FieldOperation.REMOVE:
        result["value"] = change.value
    return result


def _change_dict(change: Change) -> Dict[str, Any]:
    result = {
        "type": "change",
        "op": change.operation.value,
        "key": list(change.key),
    }
    if change.old_line is not None:
        result["old_line"] = change.old_line
    if change.new_line is not None:
        result["new_line"] = change.new_line
    if change.patch is not None:
        result["patch"] = [_field_change_dict(field_change) for field_change in change.patch]
    return result


def _duplicate_dict(duplicate: Duplicate) -> Dict[str, Any]:
    return {
        "type": "duplicate",
        "source": duplicate.source,
        "key": list(duplicate.key),
        "selected_line": duplicate.selected_line,
        "discarded_line": duplicate.discarded_line,
        "content_equal": duplicate.content_equal,
    }


def _schema_profile_dict(profile: SchemaFieldProfile) -> Dict[str, Any]:
    return {
        "parent_objects": profile.parent_objects,
        "present": profile.present,
        "missing": profile.missing,
        "nulls": profile.nulls,
        "types": profile.type_counts,
    }


def _schema_change_dict(change: SchemaChange) -> Dict[str, Any]:
    result = {
        "type": "schema_change",
        "op": change.operation.value,
        "path": change.path,
    }
    if change.old is not None:
        result["old"] = _schema_profile_dict(change.old)
    if change.new is not None:
        result["new"] = _schema_profile_dict(change.new)
    return result


def _schema_summary_dict(summary: SchemaSummary) -> Dict[str, int]:
    return {
        "fields_added": summary.fields_added,
        "fields_removed": summary.fields_removed,
        "types_changed": summary.types_changed,
        "nullability_changed": summary.nullability_changed,
        "requiredness_changed": summary.requiredness_changed,
    }


def _write_text(result: DiffResult):
    summary = result.summary
    print("Records:")
    print("  equal:     {:,}".format(summary.equal))
    print("  added:     {:,}".format(summary.added))
    print("  deleted:   {:,}".format(summary.deleted))
    print("  modified:  {:,}".format(summary.modified))
    print("  OLD duplicates:  {:,}".format(summary.old_duplicates))
    print("  NEW duplicates:  {:,}".format(summary.new_duplicates))
    if summary.schema is not None:
        print()
        print("Observed schema:")
        print("  fields added:          {:,}".format(summary.schema.fields_added))
        print("  fields removed:        {:,}".format(summary.schema.fields_removed))
        print("  type changes:          {:,}".format(summary.schema.types_changed))
        print("  nullability changes:   {:,}".format(summary.schema.nullability_changed))
        print("  requiredness changes:  {:,}".format(summary.schema.requiredness_changed))


def _write_details(result: DiffResult, path: Union[str, os.PathLike]):

    def events():
        metadata = {
            "type": "meta",
            "key": list(result.config.key),
            "ignore": list(result.config.ignore),
            "where": result.config.where,
            "duplicates": result.config.duplicates.value,
            "missing_key": result.config.missing_key.value,
        }
        if result.config.schema_diff:
            metadata["schema_diff"] = True
            metadata["schema_ignore"] = list(result.config.schema_ignore)
        if result.config.field_diff:
            metadata["field_diff"] = True
        yield metadata

        if result.config.schema_diff:
            for schema_change in result.schema_changes():
                yield _schema_change_dict(schema_change)

        for duplicate in result.duplicates():
            yield _duplicate_dict(duplicate)

        # Count fields during the change pass so the details report reads patches once.
        for change in result._changes_with_field_counts():
            yield _change_dict(change)

        if result.config.field_diff:
            for field in result.field_summary():
                yield {
                    "type": "field_summary",
                    "path": field.path,
                    "added": field.added,
                    "removed": field.removed,
                    "replaced": field.replaced,
                }

        summary = {
            "type": "summary",
            "equal": result.equal,
            "added": result.added,
            "deleted": result.deleted,
            "modified": result.modified,
            "old_duplicates": result.old_duplicates,
            "new_duplicates": result.new_duplicates,
        }
        if result.schema_summary is not None:
            summary["schema"] = _schema_summary_dict(result.schema_summary)
        yield summary

    jsonl.dump(events(), path, cls=_canonical_text)


def _same_file(first: str, second: str) -> bool:
    # A missing path cannot alias an input: a missing input fails before any write.
    try:
        return os.path.samefile(first, second)
    except (OSError, ValueError):
        return False


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str):
        self.print_usage(sys.stderr)
        self.exit(3, "{}: error: {}\n".format(self.prog, message))


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="jsonl-diff")
    parser.add_argument("old")
    parser.add_argument("new")
    parser.add_argument("--key", action="append", required=True)
    parser.add_argument("--ignore", action="append", default=[])
    parser.add_argument("--where")
    parser.add_argument(
        "--duplicates",
        choices=tuple(policy.value for policy in DuplicatePolicy),
        default=DuplicatePolicy.ERROR.value,
    )
    parser.add_argument(
        "--missing-key",
        choices=tuple(policy.value for policy in MissingKeyPolicy),
        default=MissingKeyPolicy.ERROR.value,
    )
    parser.add_argument("--details", metavar="FILE")
    parser.add_argument("--field-diff", action="store_true")
    parser.add_argument("--schema-diff", action="store_true")
    parser.add_argument("--schema-ignore", action="append", default=[], metavar="POINTER")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--format", choices=("jsonl", "json"), default="jsonl")
    return parser


def _run_comparison(arguments: argparse.Namespace, old: Any, new: Any, keys: Tuple[str, ...]) -> int:
    with diff(
        old,
        new,
        key=keys,
        ignore=arguments.ignore,
        where=arguments.where,
        duplicates=arguments.duplicates,
        missing_key=arguments.missing_key,
        schema_diff=arguments.schema_diff,
        schema_ignore=arguments.schema_ignore,
        format=arguments.format,
        field_diff=arguments.field_diff,
    ) as result:
        if arguments.details:
            _write_details(result, arguments.details)
        if not arguments.quiet:
            _write_text(result)
            sys.stdout.flush()
        return int(result.has_issues)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the jsonl-diff command-line interface."""
    parser = _parser()
    arguments = parser.parse_args(argv)

    if arguments.old == "-" and arguments.new == "-":
        parser.error("OLD and NEW cannot both read from stdin")
    if arguments.schema_ignore and not arguments.schema_diff:
        parser.error("--schema-ignore requires --schema-diff")
    if arguments.details is not None and not arguments.details.strip():
        parser.error("--details requires a non-empty file path")
    if arguments.field_diff and arguments.details is None:
        parser.error("--field-diff requires --details")
    if arguments.details is not None:
        for label, source in (("OLD", arguments.old), ("NEW", arguments.new)):
            if source != "-" and _same_file(arguments.details, source):
                parser.error("--details must not be the same file as {}".format(label))

    old = sys.stdin.buffer if arguments.old == "-" else arguments.old
    new = sys.stdin.buffer if arguments.new == "-" else arguments.new
    keys = tuple(name.strip() for item in arguments.key for name in item.split(","))

    try:
        return _run_comparison(arguments, old, new, keys)
    except ConfigurationError as error:
        parser.error(str(error))
    except (InputError, ResourceError) as error:
        print("ERROR: {}".format(error), file=sys.stderr)
        return 2
    except OSError as error:
        print("ERROR: output failed ({})".format(type(error).__name__), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ERROR: interrupted", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
