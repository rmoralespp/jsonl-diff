import bz2
import functools
import gzip
import http.server
import io
import lzma
import sys
import threading

import pytest

import jsonl_diff
from jsonl_diff import ConfigurationError, InputError, ResourceError, Summary, diff


def _write_gzip(path, content):
    with gzip.open(path, "wb") as stream:
        stream.write(content)


def _write_bzip2(path, content):
    with bz2.open(path, "wb") as stream:
        stream.write(content)


def _write_xz(path, content):
    with lzma.open(path, "wb") as stream:
        stream.write(content)


class TestCompressedSources:
    @pytest.mark.parametrize(
        "suffix,writer",
        [
            (".jsonl.gz", _write_gzip),
            (".jsonl.bz2", _write_bzip2),
            (".jsonl.xz", _write_xz),
        ],
    )
    def test_supported_compression_is_decompressed_transparently(self, tmp_path, suffix, writer):
        # Arrange
        old = tmp_path / ("old" + suffix)
        new = tmp_path / ("new" + suffix)
        writer(old, b'{"id":1,"value":"same"}\n')
        writer(new, b'{"value":"same","id":1}\n')

        # Act
        with diff(old, new, key="id") as result:
            summary = result.summary

        # Assert
        assert summary == Summary(equal=1, added=0, deleted=0, modified=0)

    @pytest.mark.skipif(sys.version_info < (3, 14), reason="py-jsonl requires Python 3.14 for zstd")
    def test_zstandard_is_decompressed_transparently(self, tmp_path):
        # Arrange
        from compression import zstd

        old = tmp_path / "old.jsonl.zst"
        new = tmp_path / "new.jsonl.zst"
        old.write_bytes(zstd.compress(b'{"id":1,"value":"same"}\n'))
        new.write_bytes(zstd.compress(b'{"value":"same","id":1}\n'))

        # Act
        with diff(old, new, key="id") as result:
            summary = result.summary

        # Assert
        assert summary == Summary(equal=1, added=0, deleted=0, modified=0)


class TestStreamSources:
    def test_binary_file_like_sources_are_supported(self):
        # Arrange
        old = io.BytesIO(b'{"id":1,"value":"same"}\n')
        new = io.BytesIO(b'{"value":"same","id":1}\n')

        # Act
        with diff(old, new, key="id") as result:
            summary = result.summary

        # Assert
        assert summary == Summary(equal=1, added=0, deleted=0, modified=0)

    def test_json_array_file_like_sources_are_supported(self):
        # Arrange
        old = io.BytesIO(b'[{"id":1,"value":"same"}]')
        new = io.BytesIO(b'[{"value":"same","id":1}]')

        # Act
        with diff(old, new, key="id", format="json") as result:
            summary = result.summary

        # Assert
        assert summary == Summary(equal=1, added=0, deleted=0, modified=0)
        assert not old.closed
        assert not new.closed


class TestJsonArraySources:
    def test_json_arrays_are_streamed_incrementally(self, tmp_path):
        # Arrange
        old = tmp_path / "old.json"
        new = tmp_path / "new.json"
        old.write_text('[{"id":1,"value":"same"},{"id":2}]', encoding="utf-8")
        new.write_text('[{"id":2},{"id":3}]', encoding="utf-8")

        # Act
        with diff(old, new, key="id", format="json") as result:
            summary = result.summary

        # Assert
        assert summary == Summary(equal=1, added=1, deleted=1, modified=0)

    @pytest.mark.parametrize(
        "suffix,writer",
        [
            (".json.gz", _write_gzip),
            (".json.bz2", _write_bzip2),
            (".json.xz", _write_xz),
        ],
    )
    def test_compressed_json_arrays_are_supported(self, tmp_path, suffix, writer):
        # Arrange
        old = tmp_path / ("old" + suffix)
        new = tmp_path / ("new" + suffix)
        writer(old, b'[{"id":1,"value":"same"}]')
        writer(new, b'[{"value":"same","id":1}]')

        # Act
        with diff(old, new, key="id", format="json") as result:
            summary = result.summary

        # Assert
        assert summary == Summary(equal=1, added=0, deleted=0, modified=0)

    def test_json_format_rejects_non_array_documents(self, tmp_path):
        # Arrange
        old = tmp_path / "old.json"
        new = tmp_path / "new.json"
        old.write_text('{"id":1}', encoding="utf-8")
        new.write_text("[]", encoding="utf-8")

        # Act / Assert
        with pytest.raises(InputError, match="invalid input"):
            with diff(old, new, key="id", format="json"):
                pass

    @pytest.mark.parametrize("content", ['[{"id":1}', '[{"id":1}] trailing'])
    def test_json_format_rejects_malformed_documents(self, tmp_path, content):
        # Arrange
        old = tmp_path / "old.json"
        new = tmp_path / "new.json"
        old.write_text(content, encoding="utf-8")
        new.write_text("[]", encoding="utf-8")

        # Act / Assert
        with pytest.raises(InputError, match="invalid input"):
            with diff(old, new, key="id", format="json"):
                pass


class _SilentHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, message, *args):
        pass


class TestHttpSources:
    def test_http_jsonl_sources_are_supported(self, tmp_path):
        # Arrange
        old = tmp_path / "old.jsonl"
        old.write_text('{"id":1}\n', encoding="utf-8")
        new = tmp_path / "new.jsonl"
        new.write_text('{"id":1}\n', encoding="utf-8")
        handler = functools.partial(_SilentHandler, directory=str(tmp_path))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()

        # Act
        try:
            port = server.server_address[1]
            with diff(
                "http://127.0.0.1:{}/old.jsonl".format(port),
                "http://127.0.0.1:{}/new.jsonl".format(port),
                key="id",
            ) as result:
                summary = result.summary
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        # Assert
        assert summary == Summary(equal=1, added=0, deleted=0, modified=0)

    def test_http_json_array_sources_are_supported(self, tmp_path):
        # Arrange
        old = tmp_path / "old.json"
        old.write_text('[{"id":1,"value":"same"}]', encoding="utf-8")
        new = tmp_path / "new.json"
        new.write_text('[{"value":"same","id":1}]', encoding="utf-8")
        handler = functools.partial(_SilentHandler, directory=str(tmp_path))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()

        # Act
        try:
            port = server.server_address[1]
            with diff(
                "http://127.0.0.1:{}/old.json".format(port),
                "http://127.0.0.1:{}/new.json".format(port),
                key="id",
                format="json",
            ) as result:
                summary = result.summary
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        # Assert
        assert summary == Summary(equal=1, added=0, deleted=0, modified=0)


class TestTemporaryStorage:
    @pytest.mark.parametrize("limit", [0, -1, "invalid"])
    def test_invalid_max_temp_is_rejected(self, write_jsonl, limit):
        # Arrange
        old = write_jsonl("old.jsonl", [])
        new = write_jsonl("new.jsonl", [])

        # Act / Assert
        with pytest.raises(ConfigurationError):
            diff(old, new, key="id", max_temp=limit)

    def test_exceeded_max_temp_raises_resource_error_and_cleans_workspace(
        self,
        write_jsonl,
    ):
        # Arrange
        payload = "x" * 1024
        old = write_jsonl(
            "old.jsonl",
            [{"id": index, "payload": payload} for index in range(300)],
        )
        new = write_jsonl("new.jsonl", [])

        # Act
        with pytest.raises(ResourceError):
            with diff(old, new, key="id", max_temp=16 * 1024):
                pass

    def test_size_is_checked_periodically_rather_than_per_record(
        self,
        write_jsonl,
        monkeypatch,
    ):
        # Arrange: enough records to span several check intervals, but well
        # under max_temp, so no ResourceError interferes with the count.
        interval = jsonl_diff._SIZE_CHECK_INTERVAL
        record_count = interval * 2
        old = write_jsonl("old.jsonl", [{"id": index} for index in range(record_count)])
        new = write_jsonl("new.jsonl", [])
        calls = []
        original_check_size = jsonl_diff.DiffResult._check_size

        def counting_check_size(self):
            calls.append(1)
            return original_check_size(self)

        monkeypatch.setattr(jsonl_diff.DiffResult, "_check_size", counting_check_size)

        # Act
        with diff(old, new, key="id", max_temp=10 * 1024 * 1024):
            pass

        # Assert: two periodic checks plus a final check per side (OLD), and
        # just the final check for the empty NEW side; far fewer than
        # `record_count` calls.
        assert len(calls) == (record_count // interval + 1) + 1
        assert len(calls) < record_count

    def test_input_error_is_reported(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", ['{"id":1', '{"id":2}'])
        new = write_jsonl("new.jsonl", [])

        # Act / Assert
        with pytest.raises(InputError):
            with diff(old, new, key="id"):
                pass
