import bz2
import functools
import gzip
import http.server
import io
import lzma
import sys
import threading

import pytest

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

    def test_input_error_is_reported(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", ['{"id":1', '{"id":2}'])
        new = write_jsonl("new.jsonl", [])

        # Act / Assert
        with pytest.raises(InputError):
            with diff(old, new, key="id"):
                pass
