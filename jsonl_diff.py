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
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional, Sequence, Tuple, Union

import jmespath
import jsonl

JsonScalar = Union[str, Decimal, bool, None]
Number = Union[Decimal, int, float]
IdentityKey = Tuple[JsonScalar, ...]

_MISSING = object()

# Avoid per-record filesystem scans: SQLite already enforces the main size limit.
# Check periodically and once after each side finishes to catch extra temp/journal growth.
_SIZE_CHECK_INTERVAL = 1024


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


@dataclass(frozen=True)
class SourceLocation:
    """A physical source location."""

    source: str
    line: int


@dataclass(frozen=True)
class Summary:
    """Comparison totals."""

    equal: int
    added: int
    deleted: int
    modified: int

    @property
    def different(self) -> bool:
        """Return whether any changed records were found."""
        return bool(self.added or self.deleted or self.modified)


@dataclass(frozen=True)
class DiffConfig:
    """Normalized comparison configuration."""

    key: Tuple[str, ...]
    ignore: Tuple[str, ...] = ()
    where: Optional[str] = None
    duplicates: DuplicatePolicy = DuplicatePolicy.ERROR
    max_temp: Optional[int] = None
    where_expression: Any = None


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
    trailing_zeros = 0
    while len(digits) - trailing_zeros > 1 and digits[-trailing_zeros - 1] == 0:
        trailing_zeros += 1

    digits = digits[:-trailing_zeros] if trailing_zeros else digits
    exponent += trailing_zeros
    mantissa = str(digits[0])
    if len(digits) > 1:
        mantissa += "." + "".join(str(digit) for digit in digits[1:])

    scientific_exponent = exponent + len(digits) - 1
    return "{}{}e{:+d}".format("-" if sign else "", mantissa, scientific_exponent)


def _canonical_text(value: Any, ensure_ascii: bool = False) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (Decimal, int, float)):
        return _number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=ensure_ascii)
    if isinstance(value, (list, tuple)):
        return "[{}]".format(",".join(_canonical_text(item, ensure_ascii) for item in value))
    if isinstance(value, dict):
        if any(not isinstance(name, str) for name in value):
            raise ValueError("JSON object property names must be strings")
        members = (
            "{}:{}".format(
                json.dumps(name, ensure_ascii=ensure_ascii),
                _canonical_text(value[name], ensure_ascii),
            )
            for name in sorted(value)
        )
        return "{{{}}}".format(",".join(members))
    raise ValueError("unsupported JSON value type: {}".format(type(value).__name__))


def _canonical(value: Any) -> bytes:
    return _canonical_text(value).encode("utf-8")


def _pointer_child(path: str, token: Union[str, int]) -> str:
    escaped = str(token).replace("~", "~0").replace("/", "~1")
    return "{}/{}".format(path, escaped)


def _structural_changes(old: Any, new: Any, path: str = "") -> Iterator[Dict[str, Any]]:
    if type(old) is not type(new):
        yield {"path": path, "old": old, "new": new}
        return
    if isinstance(old, dict):
        for name in sorted(old.keys() | new.keys()):
            child_path = _pointer_child(path, name)
            if name not in old:
                yield {"path": child_path, "new": new[name]}
            elif name not in new:
                yield {"path": child_path, "old": old[name]}
            else:
                yield from _structural_changes(old[name], new[name], child_path)
        return
    if isinstance(old, list):
        common_length = min(len(old), len(new))
        for index in range(common_length):
            yield from _structural_changes(old[index], new[index], _pointer_child(path, index))
        for index in range(common_length, len(old)):
            yield {"path": _pointer_child(path, index), "old": old[index]}
        for index in range(common_length, len(new)):
            yield {"path": _pointer_child(path, index), "new": new[index]}
        return
    if old != new:
        yield {"path": path, "old": old, "new": new}


def _fingerprint(canonical: bytes) -> Tuple[int, bytes]:
    return len(canonical), hashlib.sha256(canonical).digest()


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
        self._connection.execute("PRAGMA temp_store = FILE")
        if self.config.max_temp is not None:
            pages = max(1, self.config.max_temp // 4096)
            self._connection.execute("PRAGMA page_size = 4096")
            self._connection.execute("PRAGMA max_page_count = {}".format(pages))

    def _create_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE records (
                side INTEGER NOT NULL,
                identity BLOB NOT NULL,
                line INTEGER NOT NULL,
                length INTEGER NOT NULL,
                digest BLOB NOT NULL,
                content BLOB,
                PRIMARY KEY (side, identity)
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
    def different(self) -> bool:
        return self._require_summary().different

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

    def _insert_records(self, records: Iterable[Any], side: int, source: str) -> None:
        cursor = self._connection.cursor()
        insert = {
            DuplicatePolicy.ERROR: "INSERT",
            DuplicatePolicy.FIRST: "INSERT OR IGNORE",
            DuplicatePolicy.LAST: "INSERT OR REPLACE",
        }[self.config.duplicates]
        for line, record in enumerate(records, start=1):
            if not isinstance(record, dict):
                raise InputError("each record must be a JSON object", source, line)
            if self._where is not None:
                try:
                    matched = bool(self._where.search(record))
                except (TypeError, ValueError) as error:
                    raise InputError(str(error), source, line) from error
                if not matched:
                    continue
            try:
                key = _identity(record, self.config.key)
                normalized = _remove_ignored(record, self._ignore_tree)
                canonical = _canonical(normalized)
                identity = _canonical(list(key))
                length, digest = _fingerprint(canonical)
            except (TypeError, ValueError) as error:
                raise InputError(str(error), source, line) from error
            try:
                cursor.execute(
                    "{} INTO records VALUES (?, ?, ?, ?, ?, NULL)".format(insert),
                    (
                        side,
                        identity,
                        line,
                        length,
                        digest,
                    ),
                )
            except sqlite3.IntegrityError as error:
                first = cursor.execute(
                    "SELECT line FROM records WHERE side = ? AND identity = ?",
                    (side, identity),
                ).fetchone()[0]
                raise DuplicateKeyError(key, source, (first, line)) from error
            except sqlite3.DatabaseError as error:
                raise ResourceError("could not write the temporary index") from error
            if self.config.max_temp is not None and line % _SIZE_CHECK_INTERVAL == 0:
                self._check_size()
        self._check_size()

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
        row = self._connection.execute(
            """
            SELECT
                (SELECT COUNT(*)
                 FROM records AS o JOIN records AS n USING (identity)
                 WHERE o.side = 0 AND n.side = 1
                   AND o.length = n.length
                   AND o.digest = n.digest),
                (SELECT COUNT(*)
                 FROM records AS n
                 WHERE n.side = 1 AND NOT EXISTS (
                     SELECT 1 FROM records AS o
                     WHERE o.side = 0 AND o.identity = n.identity
                 )),
                (SELECT COUNT(*)
                 FROM records AS o
                 WHERE o.side = 0 AND NOT EXISTS (
                     SELECT 1 FROM records AS n
                     WHERE n.side = 1 AND n.identity = o.identity
                 )),
                (SELECT COUNT(*)
                 FROM records AS o JOIN records AS n USING (identity)
                 WHERE o.side = 0 AND n.side = 1
                   AND (
                       o.length != n.length
                       OR o.digest != n.digest
                   ))
            """,
        ).fetchone()
        return Summary(*(int(value or 0) for value in row))

    def _prepare_field_diff(self) -> None:
        try:
            for side, source in enumerate((self._old, self._new)):
                self._retrieve_field_records(source, side)
        except sqlite3.DatabaseError as error:
            raise ResourceError("could not store records for field diff") from error

    def _field_diff_targets(self, side: int) -> Iterator[Tuple[int, bytes, int, bytes]]:
        yield from self._connection.execute(
            """
            SELECT current.line, current.identity, current.length, current.digest
            FROM records AS current
            JOIN records AS other
              ON current.identity = other.identity
             AND other.side = ?
            WHERE current.side = ?
              AND (
                  current.length != other.length
                  OR current.digest != other.digest
              )
            ORDER BY current.line
            """,
            (1 - side, side),
        )

    def _retrieve_field_records(
        self,
        source: Any,
        side: int,
    ) -> None:
        name = "OLD" if side == 0 else "NEW"
        if hasattr(source, "read"):
            try:
                source.seek(0)
            except (AttributeError, OSError) as error:
                raise InputError("field diff requires a source that can be read again", name) from error

        error_line = [None]

        def on_error(line: int, error: Exception) -> None:
            error_line[0] = line

        try:
            self._store_field_records(source, side, name, on_error)
        except JsonlDiffError:
            raise
        except sqlite3.DatabaseError:
            raise
        except (OSError, EOFError, TypeError, ValueError, RuntimeError) as error:
            line = error_line[0]
            message = "invalid input ({})".format(type(error).__name__)
            raise InputError(message, name, line) from error
        self._check_size()

    def _store_field_records(self, source: Any, side: int, name: str, on_error: Any) -> None:
        with self._connection:
            targets = self._field_diff_targets(side)
            target = next(targets, None)
            if target is None:
                return
            for line, record in enumerate(_records(source, on_error), start=1):
                target_line, identity, expected_length, expected_digest = target
                if line < target_line:
                    continue
                if line != target_line:
                    raise InputError("source changed while generating field diff", name, target_line)
                if not isinstance(record, dict):
                    raise InputError("each record must be a JSON object", name, line)
                normalized = _remove_ignored(record, self._ignore_tree)
                canonical = _canonical(normalized)
                if _fingerprint(canonical) != (expected_length, expected_digest):
                    raise InputError("source changed while generating field diff", name, line)
                self._connection.execute(
                    "UPDATE records SET content = ? WHERE side = ? AND identity = ?",
                    (canonical, side, identity),
                )
                target = next(targets, None)
                if target is None:
                    break
            if target is not None:
                raise InputError("source changed while generating field diff", name, target[0])

    def _field_changes(self, change: Change) -> Iterator[Dict[str, Any]]:
        identity = _canonical(list(change.key))
        rows = self._connection.execute(
            """
            SELECT side, content
            FROM records
            WHERE identity = ?
            ORDER BY side
            """,
            (identity,),
        ).fetchall()
        if len(rows) != 2 or rows[0][0] != 0 or rows[1][0] != 1:
            raise RuntimeError("field diff records are unavailable")
        old = _decode_json(rows[0][1].decode("utf-8"))
        new = _decode_json(rows[1][1].decode("utf-8"))
        yield from _structural_changes(old, new)


def _configuration(
    key: Union[str, Sequence[str]],
    ignore: Sequence[str],
    where: Optional[str],
    duplicates: Union[str, DuplicatePolicy],
    max_temp: Optional[int],
) -> DiffConfig:
    keys = (key,) if isinstance(key, str) else tuple(key)
    if not keys or any(not isinstance(name, str) or not name for name in keys):
        raise ConfigurationError("at least one non-empty identity field is required")
    if len(set(keys)) != len(keys):
        raise ConfigurationError("identity fields must not be repeated")
    keys = tuple(sorted(keys))
    ignores = tuple(dict.fromkeys(ignore))
    _ignore_tree(ignores, keys)
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
    return DiffConfig(keys, ignores, where, duplicate_policy, max_temp, where_expression)


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
    """Create a context-managed, disk-backed comparison."""
    config = _configuration(key, ignore, where, duplicates, max_temp)
    return DiffResult(old, new, config)


def _change_dict(
    change: Change,
    field_changes: Optional[Iterable[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    result = {
        "type": "change",
        "op": change.operation.value,
        "key": list(change.key),
    }
    if change.old_line is not None:
        result["old_line"] = change.old_line
    if change.new_line is not None:
        result["new_line"] = change.new_line
    if field_changes is not None:
        result["changes"] = list(field_changes)
    return result


def _write_text(result: DiffResult) -> None:
    summary = result.summary
    print("Records:")
    print("  equal:     {:,}".format(summary.equal))
    print("  added:     {:,}".format(summary.added))
    print("  deleted:   {:,}".format(summary.deleted))
    print("  modified:  {:,}".format(summary.modified))


def _write_details(
    result: DiffResult,
    path: Union[str, os.PathLike],
    field_diff: bool = False,
) -> None:
    if field_diff:
        result._prepare_field_diff()

    def events() -> Iterator[Dict[str, Any]]:
        metadata = {
            "type": "meta",
            "key": list(result.config.key),
            "ignore": list(result.config.ignore),
            "where": result.config.where,
            "duplicates": result.config.duplicates.value,
        }
        if field_diff:
            metadata["field_diff"] = True
        yield metadata
        for change in result.changes():
            changes = None
            if field_diff and change.operation == ChangeOperation.MODIFIED:
                changes = result._field_changes(change)
            yield _change_dict(change, changes)
        yield {
            "type": "summary",
            "equal": result.equal,
            "added": result.added,
            "deleted": result.deleted,
            "modified": result.modified,
        }

    jsonl.dump(events(), path, cls=_canonical_text)


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
    parser.add_argument("--field-diff", action="store_true")
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
    ) as result:
        if arguments.details:
            _write_details(result, arguments.details, arguments.field_diff)
        if not arguments.quiet:
            _write_text(result)
            sys.stdout.flush()
        return 1 if result.different else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the jsonl-diff command-line interface."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.old == "-" and arguments.new == "-":
        parser.error("OLD and NEW cannot both read from stdin")
    if arguments.field_diff and not arguments.details:
        parser.error("--field-diff requires --details FILE")
    if arguments.field_diff and (arguments.old == "-" or arguments.new == "-"):
        parser.error("--field-diff cannot be used with stdin")
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
