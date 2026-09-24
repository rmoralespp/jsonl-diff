"""Strict, disk-backed reconciliation of JSON Lines datasets."""

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import tempfile
import warnings
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from json.encoder import encode_basestring as _encode_basestring
from json.encoder import encode_basestring_ascii as _encode_basestring_ascii
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, Optional, Sequence, Tuple, Union

import jmespath
import jsonl

JsonScalar = Union[str, Decimal, bool, None]
Number = Union[Decimal, int, float]
IdentityKey = Tuple[JsonScalar, ...]

_MISSING = object()

_DIGITS = ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9")

# Avoid per-record filesystem scans: SQLite already enforces the main size limit.
# Check periodically and once after each side finishes to catch extra temp/journal growth.
_SIZE_CHECK_INTERVAL = 1024
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
            _canonical_text(list(key), ensure_ascii=True),
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


class SchemaChangeOperation(str, Enum):
    """An observed schema change classification."""

    FIELD_ADDED = "field_added"
    FIELD_REMOVED = "field_removed"
    TYPES_CHANGED = "types_changed"
    NULLABILITY_CHANGED = "nullability_changed"
    REQUIREDNESS_CHANGED = "requiredness_changed"


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
        return bool(
            self.fields_added
            or self.fields_removed
            or self.types_changed
            or self.nullability_changed
            or self.requiredness_changed,
        )


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
    max_temp: Optional[int] = None
    where_expression: Any = None
    schema_diff: bool = False
    schema_ignore: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Change:
    """One added, deleted, or modified identity."""

    operation: ChangeOperation
    key: IdentityKey
    old: Optional[SourceLocation]
    new: Optional[SourceLocation]

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


def _reject_constant(value: str) -> None:
    raise ValueError("invalid JSON number {!r}".format(value))


def _object(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate property {!r}".format(name))
        result[name] = value
    return result


def _decode_json(value: str) -> Any:
    return json.loads(
        value,
        parse_int=Decimal,
        parse_float=Decimal,
        parse_constant=_reject_constant,
        object_pairs_hook=_object,
    )


def _number(value: Number) -> str:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numbers are not valid JSON")
        value = Decimal(str(value))
    elif isinstance(value, int):
        value = Decimal(value)
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("non-finite numbers are not valid JSON")
    if value.is_zero():
        return "0"

    sign, digits, exponent = value.as_tuple()
    count = len(digits)
    trailing_zeros = 0
    while count - trailing_zeros > 1 and digits[count - trailing_zeros - 1] == 0:
        trailing_zeros += 1
    if trailing_zeros:
        digits = digits[:count - trailing_zeros]
        exponent += trailing_zeros
        count -= trailing_zeros

    if count == 1:
        mantissa = _DIGITS[digits[0]]
    else:
        text = "".join([_DIGITS[digit] for digit in digits])
        mantissa = text[0] + "." + text[1:]

    scientific_exponent = exponent + count - 1
    return "{}{}e{:+d}".format("-" if sign else "", mantissa, scientific_exponent)


def _details_number(value: Number) -> str:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite numbers are not valid JSON")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError("non-finite numbers are not valid JSON")
    return str(value).lower()


def _digest_number(value: Number) -> str:
    # Formats numbers for the content fingerprint only (hashed, never decoded or
    # displayed), so it just needs value-consistent bytes. Ordinary-magnitude
    # integrals use the fast plain-integer form; classifying by value keeps
    # `1`, `1.0` and `1e0` identical. Fractions and huge integrals fall back to
    # the exact scientific form and are never materialised as gigantic ints.
    if type(value) is Decimal and value.adjusted() < 18 and value == value.to_integral_value():
        return str(int(value))
    return _number(value)


def _require_str_key(name: Any) -> bool:
    if isinstance(name, str):
        return True
    raise ValueError("JSON object property names must be strings")


def _json_text(
    value: Any,
    escape: Callable[[str], str],
    number: Callable[[Number], str],
) -> str:
    # Checks are ordered by frequency for object-heavy payloads. String escaping
    # is delegated to CPython's C-accelerated encoder (`escape`) rather than a
    # per-value `json.dumps`.
    kind = type(value)
    if kind is str:
        return escape(value)
    if kind is dict:
        members = [
            escape(name) + ":" + _json_text(value[name], escape, number)
            for name in sorted(value)
            if type(name) is str or _require_str_key(name)
        ]
        return "{" + ",".join(members) + "}"
    if kind is list or kind is tuple:
        return "[" + ",".join([_json_text(item, escape, number) for item in value]) + "]"
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (Decimal, int, float)):
        return number(value)
    if isinstance(value, str):
        return escape(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join([_json_text(item, escape, number) for item in value]) + "]"
    if isinstance(value, dict):
        members = [
            escape(name) + ":" + _json_text(value[name], escape, number)
            for name in sorted(value)
            if type(name) is str or _require_str_key(name)
        ]
        return "{" + ",".join(members) + "}"
    raise ValueError("unsupported JSON value type: {}".format(type(value).__name__))


def _canonical_text(value: Any, ensure_ascii: bool = False) -> str:
    escape = _encode_basestring_ascii if ensure_ascii else _encode_basestring
    return _json_text(value, escape, _number)


def _details_text(value: Any, ensure_ascii: bool = False) -> str:
    escape = _encode_basestring_ascii if ensure_ascii else _encode_basestring
    return _json_text(value, escape, _details_number)


def _canonical(value: Any) -> bytes:
    return _canonical_text(value).encode("utf-8")


def _content_canonical(value: Any) -> bytes:
    # Bytes hashed for the content fingerprint. Uses the fast digest-only number
    # formatter; identity encoding keeps `_canonical` so key notation round-trips.
    return _json_text(value, _encode_basestring, _digest_number).encode("utf-8")


def _fingerprint(canonical: bytes) -> Tuple[int, bytes]:
    return len(canonical), hashlib.sha256(canonical).digest()


def _pointer_child(path: str, token: str) -> str:
    escaped = token.replace("~", "~0").replace("/", "~1")
    return "{}/{}".format(path, escaped)


def _schema_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Decimal):
        return "integer" if value == value.to_integral_value() else "number"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "integer" if value.is_integer() else "number"
    raise ValueError("unsupported JSON value type: {}".format(type(value).__name__))


def _profile_schema(
    record: Dict[str, Any],
    ignore_tree: Dict[str, Any],
    fields: Dict[str, Any],
    objects: Dict[str, int],
) -> None:
    stack = [("", record, ignore_tree)]
    while stack:
        path, value, tree = stack.pop()
        objects[path] = objects.get(path, 0) + 1
        for name, item in value.items():
            child_tree = tree.get(name)
            if child_tree is not None and _MISSING in child_tree:
                continue
            if child_tree and isinstance(item, list):
                raise ValueError("schema ignore paths may not traverse arrays")
            child_path = _pointer_child(path, name)
            counts = fields.get(child_path)
            if counts is None:
                counts = [path, 0, 0, 0, 0, 0, 0, 0, 0]
                fields[child_path] = counts
            counts[1] += 1
            if item is None:
                counts[2] += 1
            else:
                counts[_SCHEMA_TYPE_INDEXES[_schema_type(item)]] += 1
            if isinstance(item, dict):
                stack.append((child_path, item, child_tree or {}))


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
    tree = {}
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


def _identity(record: Dict[str, Any], keys: Sequence[str]) -> IdentityKey:
    composite = len(keys) > 1
    values = []
    for name in keys:
        if name not in record:
            raise ValueError("missing identity field {!r}".format(name))
        value = record[name]
        if value is None:
            # Composite identities may contain null components; uniqueness is
            # still enforced on the complete identity tuple. A single-field
            # identity may not be null, since that would collapse every null
            # record into one indistinguishable identity.
            if not composite:
                raise ValueError("identity field {!r} must be a non-null scalar".format(name))
        elif not isinstance(value, (str, Decimal, bool)):
            raise ValueError("identity field {!r} must be a non-null scalar".format(name))
        values.append(value)
    return tuple(values)


def _compile_where(expression: str) -> Any:
    try:
        return jmespath.compile(expression)
    except jmespath.exceptions.JMESPathError as error:
        raise ConfigurationError(
            "invalid --where expression {!r}: {}".format(expression, error),
        ) from error


def _records(source: Any, on_error: Any) -> Iterator[Any]:
    yield from jsonl.load(
        source,
        parse_int=Decimal,
        parse_float=Decimal,
        parse_constant=_reject_constant,
        object_pairs_hook=_object,
        _on_error=on_error,
    )


class DiffResult:
    """A context-managed, disk-backed comparison result."""

    def __init__(self, old: Any, new: Any, config: DiffConfig):
        self._old = old
        self._new = new
        self.config = config
        self._ignore_tree = _ignore_tree(config.ignore, config.key)
        self._schema_ignore_tree = _ignore_tree(config.schema_ignore, ())
        # Compiled during configuration validation and reused for every record;
        # never compile inside the per-record processing loop.
        self._where = config.where_expression
        self._workspace = None
        self._connection = None
        self._summary_value = None
        self._started = False

    def _open(self) -> None:
        try:
            self._workspace = tempfile.TemporaryDirectory(prefix="jsonl-diff-")
            self._connection = sqlite3.connect(os.path.join(self._workspace.name, "index.sqlite3"))
            self._configure_database()
            self._create_schema()
        except (OSError, sqlite3.Error) as error:
            self.close()
            raise ResourceError("could not create the temporary index") from error

    def _configure_database(self) -> None:
        self._connection.execute("PRAGMA journal_mode = OFF")
        self._connection.execute("PRAGMA synchronous = OFF")
        self._connection.execute("PRAGMA temp_store = FILE")
        if self.config.max_temp is not None:
            pages = max(1, self.config.max_temp // 4096)
            self._connection.execute("PRAGMA page_size = 4096")
            self._connection.execute("PRAGMA max_page_count = {}".format(pages))
        else:
            # A larger page cache speeds bulk index writes and the summary read.
            # Skipped under max_temp so on-disk growth stays observable and the
            # workspace memory footprint remains bounded.
            self._connection.execute("PRAGMA cache_size = -65536")

    def _create_schema(self) -> None:
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
        query = """
            SELECT identity, line, length, digest
            FROM records
            WHERE side = ?
            ORDER BY identity
        """
        old_records = iter(self._connection.execute(query, (0,)))
        new_records = iter(self._connection.execute(query, (1,)))
        old_record = next(old_records, None)
        new_record = next(new_records, None)
        while old_record is not None or new_record is not None:
            comparison = (
                1
                if old_record is None
                else -1
                if new_record is None
                else (old_record[0] > new_record[0]) - (old_record[0] < new_record[0])
            )
            if comparison < 0:
                change = self._change(ChangeOperation.DELETED, old_record, None)
                old_record = next(old_records, None)
            elif comparison > 0:
                change = self._change(ChangeOperation.ADDED, None, new_record)
                new_record = next(new_records, None)
            else:
                old_signature = old_record[2:]
                new_signature = new_record[2:]
                change = (
                    None
                    if old_signature == new_signature
                    else self._change(ChangeOperation.MODIFIED, old_record, new_record)
                )
                old_record = next(old_records, None)
                new_record = next(new_records, None)
            if change is not None and (requested is None or change.operation == requested):
                yield change

    def schema_changes(
        self,
        operation: Optional[SchemaChangeOperation] = None,
    ) -> Iterator[SchemaChange]:
        """Iterate observed schema changes in deterministic order."""
        if self._connection is None:
            raise RuntimeError("the diff result is closed")
        if not self.config.schema_diff:
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
    def _changed_schema_dimensions(
        old: SchemaFieldProfile,
        new: SchemaFieldProfile,
    ) -> Iterator[SchemaChange]:
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
            source = "OLD" if side == 0 else "NEW"
            key = tuple(_decode_json(identity.decode("utf-8")))
            yield Duplicate(
                key,
                SourceLocation(source, selected_line),
                SourceLocation(source, discarded_line),
                bool(content_equal),
            )

    def _change(
        self,
        operation: ChangeOperation,
        old_record: Optional[Tuple[Any, ...]],
        new_record: Optional[Tuple[Any, ...]],
    ) -> Change:
        record = old_record or new_record
        key = tuple(_decode_json(record[0].decode("utf-8")))
        old_line = None if old_record is None else old_record[1]
        new_line = None if new_record is None else new_record[1]
        return Change(
            operation,
            key,
            None if old_line is None else SourceLocation("OLD", old_line),
            None if new_line is None else SourceLocation("NEW", new_line),
        )

    def close(self) -> None:
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
            self._open()
            self._index(self._old, 0)
            self._index(self._new, 1)
            self._summary_value = self._calculate_summary()
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def _insert_tolerated_record(
        self,
        cursor: sqlite3.Cursor,
        values: Tuple[Any, ...],
    ) -> None:
        cursor.execute(
            "INSERT OR IGNORE INTO records VALUES (?, ?, ?, ?, ?)",
            values,
        )
        if cursor.rowcount != 0:
            return
        side, identity, line, length, digest = values
        if self.config.duplicates == DuplicatePolicy.FIRST:
            cursor.execute(
                "INSERT INTO duplicate_records VALUES (?, ?, ?, ?, ?)",
                values,
            )
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

    def _insert_records(self, records: Iterable[Any], side: int, source: str) -> None:
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
                    if (
                        self.config.schema_diff or self.config.max_temp is not None
                    ) and line % _SIZE_CHECK_INTERVAL == 0:
                        if pending:
                            self._flush_record_batch(cursor, pending, source)
                        if self.config.schema_diff:
                            self._flush_schema_profile(
                                cursor,
                                side,
                                schema_fields,
                                schema_objects,
                            )
                        if self.config.max_temp is not None:
                            self._check_size()
                    continue
            try:
                key = _identity(record, self.config.key)
                normalized = _remove_ignored(record, self._ignore_tree)
                canonical = _content_canonical(normalized)
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
            if (
                self.config.schema_diff or self.config.max_temp is not None
            ) and line % _SIZE_CHECK_INTERVAL == 0:
                if pending:
                    self._flush_record_batch(cursor, pending, source)
                if self.config.schema_diff:
                    self._flush_schema_profile(cursor, side, schema_fields, schema_objects)
                if self.config.max_temp is not None:
                    self._check_size()
        if pending:
            self._flush_record_batch(cursor, pending, source)
        if self.config.schema_diff:
            self._flush_schema_profile(cursor, side, schema_fields, schema_objects)
        self._check_size()

    @staticmethod
    def _flush_record_batch(
        cursor: sqlite3.Cursor,
        pending: list,
        source: str,
    ) -> None:
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
                    key = tuple(_decode_json(row[1].decode("utf-8")))
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
        fields: Dict[str, Any],
        objects: Dict[str, int],
    ) -> None:
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
                    (side, path, counts[0], *counts[1:])
                    for path, counts in fields.items()
                ),
            )
            fields.clear()

    def _index(self, source: Any, side: int) -> None:
        name = "OLD" if side == 0 else "NEW"
        error_line = [None]

        def on_error(line: int, error: Exception) -> None:
            error_line[0] = line

        try:
            with self._connection:
                self._insert_records(_records(source, on_error), side, name)
        except JsonlDiffError:
            raise
        except (OSError, EOFError, ValueError, RuntimeError) as error:
            line = error_line[0]
            message = "invalid input ({})".format(type(error).__name__)
            raise InputError(message, name, line) from error
        except sqlite3.DatabaseError as error:
            raise ResourceError("could not build the temporary index") from error

    def _check_size(self) -> None:
        if self.config.max_temp is None:
            return
        usage = sum(
            entry.stat().st_size
            for entry in Path(self._workspace.name).iterdir()
            if entry.is_file()
        )
        if usage > self.config.max_temp:
            raise ResourceError(
                "temporary storage exceeded {}".format(self.config.max_temp),
            )

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
        total_old, total_new, matched, equal, old_dups, new_dups = (
            int(value or 0) for value in row
        )
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


def _configuration(
    key: Union[str, Sequence[str]],
    ignore: Sequence[str],
    where: Optional[str],
    duplicates: Union[str, DuplicatePolicy],
    max_temp: Optional[int],
    schema_diff: bool,
    schema_ignore: Sequence[str],
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
    if (
        max_temp is not None
        and (not isinstance(max_temp, int) or isinstance(max_temp, bool) or max_temp <= 0)
    ):
        raise ConfigurationError("max_temp must be a positive integer")
    return DiffConfig(
        key=keys,
        ignore=ignores,
        where=where,
        duplicates=duplicate_policy,
        max_temp=max_temp,
        where_expression=where_expression,
        schema_diff=schema_diff,
        schema_ignore=schema_ignores,
    )


def diff(
    old: Any,
    new: Any,
    *,
    key: Union[str, Sequence[str]],
    ignore: Sequence[str] = (),
    where: Optional[str] = None,
    duplicates: Union[str, DuplicatePolicy] = DuplicatePolicy.ERROR,
    max_temp: Optional[int] = None,
    schema_diff: bool = False,
    schema_ignore: Sequence[str] = (),
) -> DiffResult:
    """Create a context-managed, disk-backed comparison."""
    config = _configuration(
        key,
        ignore,
        where,
        duplicates,
        max_temp,
        schema_diff,
        schema_ignore,
    )
    return DiffResult(old, new, config)


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


def _write_text(result: DiffResult) -> None:
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


def _write_details(result: DiffResult, path: Union[str, os.PathLike]) -> None:
    def events() -> Iterator[Dict[str, Any]]:
        metadata = {
            "type": "meta",
            "key": list(result.config.key),
            "ignore": list(result.config.ignore),
            "where": result.config.where,
            "duplicates": result.config.duplicates.value,
        }
        if result.config.schema_diff:
            metadata["schema_diff"] = True
            metadata["schema_ignore"] = list(result.config.schema_ignore)
        yield metadata
        if result.config.schema_diff:
            for schema_change in result.schema_changes():
                yield _schema_change_dict(schema_change)
        for duplicate in result.duplicates():
            yield _duplicate_dict(duplicate)
        for change in result.changes():
            yield _change_dict(change)
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

    jsonl.dump(events(), path, cls=_details_text)


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
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
    parser.add_argument("--details", metavar="FILE")
    parser.add_argument("--schema-diff", action="store_true")
    parser.add_argument("--schema-ignore", action="append", default=[], metavar="POINTER")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--max-temp", type=int)
    return parser


def _run_comparison(arguments: argparse.Namespace, old: Any, new: Any, keys: Tuple[str, ...]) -> int:
    with diff(
        old,
        new,
        key=keys,
        ignore=arguments.ignore,
        where=arguments.where,
        duplicates=arguments.duplicates,
        max_temp=arguments.max_temp,
        schema_diff=arguments.schema_diff,
        schema_ignore=arguments.schema_ignore,
    ) as result:
        if arguments.details:
            _write_details(result, arguments.details)
        if not arguments.quiet:
            _write_text(result)
            sys.stdout.flush()
        return 1 if result.has_issues else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the jsonl-diff command-line interface."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.old == "-" and arguments.new == "-":
        parser.error("OLD and NEW cannot both read from stdin")
    if arguments.schema_ignore and not arguments.schema_diff:
        parser.error("--schema-ignore requires --schema-diff")
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
