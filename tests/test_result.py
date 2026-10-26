import io
import json
import multiprocessing
import sqlite3
import tempfile
import time
import types

import pytest

import jsonl_diff
from jsonl_diff import (
    ConfigurationError,
    InputError,
    ResourceError,
    SchemaChangeOperation,
    diff,
    main,
)

_FLUSH_INTERVAL = 1024


class _FailingCloseConnection(sqlite3.Connection):
    def close(self):
        super().close()
        raise sqlite3.OperationalError("close failed")


class _FailingReadStream:
    def read(self, size=-1):
        raise OSError("read failed")


class _FailingWriteFile:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def write(self, data):
        raise OSError("disk full")


def _raise_os_error(*args, **kwargs):
    raise OSError("unavailable")


class _ModuleProxy:
    """Override attributes of a module only as seen by ``jsonl_diff``."""

    def __init__(self, module, **overrides):
        self._module = module
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._module, name)


class TestDiffResultProperties:
    def test_has_duplicates_ok(self, write_jsonl):
        old = write_jsonl("old.jsonl", [{"id": 1}, {"id": 1}])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        with diff(old, new, key="id", duplicates="first") as result:
            assert result.has_duplicates is True

    def test_has_duplicates_na(self, write_jsonl):
        old = write_jsonl("old.jsonl", [{"id": 1}])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        with diff(old, new, key="id") as result:
            assert result.has_duplicates is False

    def test_has_schema_changes_ok(self, write_jsonl):
        old = write_jsonl("old.jsonl", [{"id": 1}])
        new = write_jsonl("new.jsonl", [{"id": 1, "extra": True}])

        with diff(old, new, key="id", schema_diff=True) as result:
            assert result.has_schema_changes is True

    def test_schema_changes_filter_without_matches_na(self, write_jsonl):
        old = write_jsonl("old.jsonl", [{"id": 1}])
        new = write_jsonl("new.jsonl", [{"id": 1, "extra": True}])

        with diff(old, new, key="id", schema_diff=True) as result:
            removed = list(result.schema_changes(SchemaChangeOperation.FIELD_REMOVED))

        assert removed == []


class TestClosedDiffResult:
    def test_changes_after_close_ko(self, write_jsonl):
        source = write_jsonl("data.jsonl", [{"id": 1}])
        with diff(source, source, key="id") as result:
            pass

        with pytest.raises(RuntimeError, match="closed"):
            list(result.changes())

    def test_duplicates_after_close_ko(self, write_jsonl):
        source = write_jsonl("data.jsonl", [{"id": 1}])
        with diff(source, source, key="id") as result:
            pass

        with pytest.raises(RuntimeError, match="closed"):
            list(result.duplicates())

    def test_schema_changes_after_close_ko(self, write_jsonl):
        source = write_jsonl("data.jsonl", [{"id": 1}])
        with diff(source, source, key="id", schema_diff=True) as result:
            pass

        with pytest.raises(RuntimeError, match="closed"):
            list(result.schema_changes())


class TestDiffResultCleanup:
    def test_connection_close_failure_warns(self, write_jsonl, monkeypatch):
        # Arrange
        source = write_jsonl("data.jsonl", [{"id": 1}])
        monkeypatch.setattr(
            jsonl_diff,
            "sqlite3",
            _ModuleProxy(sqlite3, connect=lambda path: sqlite3.connect(path, factory=_FailingCloseConnection)),
        )

        # Act / Assert
        with pytest.warns(UserWarning, match="could not close temporary database"):
            with diff(source, source, key="id"):
                pass

    def test_workspace_cleanup_failure_warns(self, write_jsonl, monkeypatch):
        # Arrange
        source = write_jsonl("data.jsonl", [{"id": 1}])

        class FailingCleanupDirectory(tempfile.TemporaryDirectory):
            def cleanup(self):
                super().cleanup()
                raise OSError("busy")

        monkeypatch.setattr(jsonl_diff, "tempfile", _ModuleProxy(tempfile, TemporaryDirectory=FailingCleanupDirectory))

        # Act / Assert
        with pytest.warns(UserWarning, match="could not remove temporary files"):
            with diff(source, source, key="id"):
                pass


class TestDiffResultResourceErrors:
    def test_workspace_creation_failure_ko(self, write_jsonl, monkeypatch):
        source = write_jsonl("data.jsonl", [{"id": 1}])
        monkeypatch.setattr(jsonl_diff, "tempfile", _ModuleProxy(tempfile, TemporaryDirectory=_raise_os_error))

        with pytest.raises(ResourceError, match="could not create the temporary index"):
            with diff(source, source, key="id"):
                pass

    def test_index_database_failure_ko(self, write_jsonl, monkeypatch):
        source = write_jsonl("data.jsonl", [{"id": 1}])

        def failing_connect(path):
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(jsonl_diff, "sqlite3", _ModuleProxy(sqlite3, connect=failing_connect))

        with pytest.raises(ResourceError, match="could not build the temporary index"):
            with diff(source, source, key="id"):
                pass

    def test_stream_staging_open_failure_ko(self, monkeypatch):
        monkeypatch.setattr(jsonl_diff, "open", _raise_os_error, raising=False)

        with pytest.raises(ResourceError, match="could not write the temporary input"):
            with diff(io.BytesIO(b'{"id":1}\n'), io.BytesIO(b'{"id":1}\n'), key="id"):
                pass

    def test_stream_staging_write_failure_ko(self, monkeypatch):
        monkeypatch.setattr(jsonl_diff, "open", lambda *args, **kwargs: _FailingWriteFile(), raising=False)

        with pytest.raises(ResourceError, match="could not write the temporary input"):
            with diff(io.BytesIO(b'{"id":1}\n'), io.BytesIO(b'{"id":1}\n'), key="id"):
                pass


class _FakeWorker:
    """Worker stand-in that ignores ``terminate()``; an unbounded ``join()`` simulates Ctrl+C."""

    def __init__(self, start_error=None):
        self.start_error = start_error
        self.alive = False
        self.calls = []

    def start(self):
        if self.start_error is not None:
            raise self.start_error
        self.alive = True

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")
        self.alive = False

    def join(self, timeout=None):
        self.calls.append("join")
        if timeout is None and self.alive:
            raise KeyboardInterrupt


class _InterruptibleProcess:
    """Real worker process whose first unbounded ``join()`` simulates Ctrl+C in the parent."""

    def __init__(self, process):
        self._process = process
        self._interrupted = False

    def __getattr__(self, name):
        return getattr(self._process, name)

    def join(self, timeout=None):
        if timeout is None and not self._interrupted:
            self._interrupted = True
            raise KeyboardInterrupt
        self._process.join(timeout)


def _use_workers(monkeypatch, workers):
    context = types.SimpleNamespace(Process=lambda **kwargs: workers.pop(0))
    monkeypatch.setattr(jsonl_diff, "_multiprocessing_context", lambda: context)


class TestWorkerShutdown:
    def test_interrupt_leaves_no_live_worker_processes(self, write_jsonl, monkeypatch):
        # Arrange
        source = write_jsonl("data.jsonl", [{"id": 1}])
        context = multiprocessing.get_context()
        processes = [context.Process(target=time.sleep, args=(60,), daemon=True) for _ in range(2)]
        _use_workers(monkeypatch, [_InterruptibleProcess(process) for process in processes])

        # Act
        with pytest.raises(KeyboardInterrupt):
            with diff(source, source, key="id"):
                pass

        # Assert
        assert not any(process.is_alive() for process in processes)
        assert all(process.exitcode != 0 for process in processes)

    def test_worker_ignoring_terminate_is_killed(self, write_jsonl, monkeypatch):
        # Arrange
        source = write_jsonl("data.jsonl", [{"id": 1}])
        workers = [_FakeWorker(), _FakeWorker()]
        _use_workers(monkeypatch, list(workers))

        # Act
        with pytest.raises(KeyboardInterrupt):
            with diff(source, source, key="id"):
                pass

        # Assert
        assert workers[1].calls == ["terminate", "join", "kill", "join"]
        assert not any(worker.alive for worker in workers)

    def test_failed_start_stops_workers_and_reports_resource_error(self, write_jsonl, monkeypatch, capsys):
        # Arrange
        source = write_jsonl("data.jsonl", [{"id": 1}])
        started = _FakeWorker()
        _use_workers(monkeypatch, [started, _FakeWorker(start_error=OSError("fork failed"))])

        # Act
        exit_code = main([str(source), str(source), "--key", "id"])

        # Assert
        assert exit_code == 2
        assert capsys.readouterr().err == "ERROR: could not start indexing workers\n"
        assert not started.alive


class TestStreamSources:
    def test_text_stream_ok(self):
        old = io.StringIO('{"id":1,"value":"a"}\n')
        new = io.StringIO('{"id":1,"value":"b"}\n')

        with diff(old, new, key="id") as result:
            assert result.modified == 1

    def test_unreadable_stream_ko(self):
        with pytest.raises(InputError, match=r"invalid input \(OSError\)") as error:
            with diff(_FailingReadStream(), io.BytesIO(b'{"id":1}\n'), key="id"):
                pass

        assert error.value.source == "OLD"


class TestIgnorePaths:
    def test_ignore_root_empty_key_ok(self, write_jsonl):
        old = write_jsonl("old.jsonl", [{"id": 1, "": "before"}])
        new = write_jsonl("new.jsonl", [{"id": 1, "": "after"}])

        with diff(old, new, key="id", ignore=["/"]) as result:
            assert result.equal == 1

    def test_ignore_path_through_scalar_keeps_value(self, write_jsonl):
        old = write_jsonl("old.jsonl", [{"id": 1, "meta": 1}])
        new = write_jsonl("new.jsonl", [{"id": 1, "meta": 2}])

        with diff(old, new, key="id", ignore=["/meta/updated"]) as result:
            assert result.modified == 1

    def test_schema_ignore_through_array_ko(self, write_jsonl):
        old = write_jsonl("old.jsonl", [{"id": 1, "items": [{"x": 1}]}])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        with pytest.raises(InputError, match="schema ignore paths may not traverse arrays"):
            with diff(old, new, key="id", schema_diff=True, schema_ignore=["/items/x"]):
                pass


class TestWhereFilter:
    def test_where_type_error_ko(self, write_jsonl):
        source = write_jsonl("data.jsonl", [{"id": 1, "value": 5}])

        with pytest.raises(InputError) as error:
            with diff(source, source, key="id", where="length(value) > `0`"):
                pass

        assert error.value.line == 1

    def test_where_excluded_record_on_schema_flush_boundary_ok(self, write_jsonl):
        # Arrange: the record on the flush boundary is filtered out.
        records = [{"id": index, "keep": index != _FLUSH_INTERVAL} for index in range(1, _FLUSH_INTERVAL + 1)]
        source = write_jsonl("data.jsonl", records)

        # Act
        with diff(source, source, key="id", where="keep", schema_diff=True) as result:
            summary = result.summary

        # Assert
        assert (summary.equal, summary.schema.fields_added) == (_FLUSH_INTERVAL - 1, 0)

    def test_where_excluded_record_on_flush_boundary_with_tolerated_duplicates_ok(self, write_jsonl):
        records = [{"id": index, "keep": index != _FLUSH_INTERVAL} for index in range(1, _FLUSH_INTERVAL + 1)]
        source = write_jsonl("data.jsonl", records)

        with diff(source, source, key="id", where="keep", duplicates="first", schema_diff=True) as result:
            assert result.equal == _FLUSH_INTERVAL - 1


class TestSchemaFlush:
    def test_tolerated_duplicates_on_flush_boundary_ok(self, write_jsonl):
        records = [{"id": index} for index in range(1, _FLUSH_INTERVAL + 1)]
        source = write_jsonl("data.jsonl", records)

        with diff(source, source, key="id", duplicates="first", schema_diff=True) as result:
            assert result.equal == _FLUSH_INTERVAL


class TestJsonFormat:
    def test_leading_whitespace_longer_than_chunk_ok(self, tmp_path):
        old = tmp_path / "old.json"
        new = tmp_path / "new.json"
        old.write_text(" " * 200_000 + '[{"id":1}]', encoding="utf-8")
        new.write_text('[{"id":1}]', encoding="utf-8")

        with diff(old, new, key="id", format="json") as result:
            assert result.equal == 1


class TestConfiguration:
    def test_repeated_identity_fields_ko(self):
        with pytest.raises(ConfigurationError, match="must not be repeated"):
            diff("old.jsonl", "new.jsonl", key=["id", "id"])

    def test_invalid_format_ko(self):
        with pytest.raises(ConfigurationError, match="invalid format"):
            diff("old.jsonl", "new.jsonl", key="id", format="xml")


class TestCliEdgeCases:
    def test_details_removed_schema_field_has_only_old_profile(self, write_jsonl, tmp_path):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1, "gone": True}])
        new = write_jsonl("new.jsonl", [{"id": 1}])
        details = tmp_path / "details.jsonl"

        # Act
        main([str(old), str(new), "--key", "id", "--schema-diff", "--quiet", "--details", str(details)])

        # Assert
        events = [json.loads(line) for line in details.read_text(encoding="utf-8").splitlines()]
        schema_change = next(event for event in events if event["type"] == "schema_change")
        assert (schema_change["op"], "old" in schema_change, "new" in schema_change) == ("field_removed", True, False)

    def test_keyboard_interrupt_ko(self, write_jsonl, monkeypatch, capsys):
        # Arrange
        source = write_jsonl("data.jsonl", [{"id": 1}])

        def interrupted(*args):
            raise KeyboardInterrupt

        monkeypatch.setattr(jsonl_diff, "_run_comparison", interrupted)

        # Act
        exit_code = main([str(source), str(source), "--key", "id"])

        # Assert
        assert (exit_code, capsys.readouterr().err) == (2, "ERROR: interrupted\n")
