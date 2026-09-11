import io
import json
from decimal import Decimal

import pytest

import jsonl_diff
from jsonl_diff import main


class _BrokenOutput:
    def write(self, value):
        raise OSError("consumer closed")


def _different_sources(write_jsonl):
    old = write_jsonl(
        "old.jsonl",
        [{"id": 1}, {"id": 2, "value": "before"}],
    )
    new = write_jsonl(
        "new.jsonl",
        [{"id": 2, "value": "after"}, {"id": 3}],
    )
    return old, new


class TestCliExitCodes:
    def test_equal_inputs_return_zero(self, write_jsonl, capsys):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        # Act
        exit_code = main([str(old), str(new), "--key", "id", "--quiet"])
        captured = capsys.readouterr()

        # Assert
        assert exit_code == 0
        assert captured == ("", "")

    def test_different_inputs_return_one(self, write_jsonl, capsys):
        # Arrange
        old, new = _different_sources(write_jsonl)

        # Act
        exit_code = main([str(old), str(new), "--key", "id", "--quiet"])
        captured = capsys.readouterr()

        # Assert
        assert exit_code == 1
        assert captured == ("", "")

    def test_input_error_returns_two_without_normal_output(self, write_jsonl, capsys):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}])
        new = write_jsonl("new.jsonl", ['{"id":'])

        # Act
        exit_code = main([str(old), str(new), "--key", "id"])
        captured = capsys.readouterr()

        # Assert
        assert exit_code == 2
        assert not captured.out

    def test_output_error_returns_two(self, write_jsonl, capsys, monkeypatch):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        # Act
        with monkeypatch.context() as patch:
            patch.setattr(jsonl_diff.sys, "stdout", _BrokenOutput())
            exit_code = main([str(old), str(new), "--key", "id"])
        captured = capsys.readouterr()

        # Assert
        assert exit_code == 2
        assert "ERROR: output failed (OSError)" in captured.err

    def test_invalid_usage_exits_with_three(self, capsys):
        # Arrange
        arguments = ["old.jsonl", "new.jsonl"]

        # Act
        with pytest.raises(SystemExit) as captured_exit:
            main(arguments)
        captured = capsys.readouterr()

        # Assert
        assert captured_exit.value.code == 3
        assert "the following arguments are required: --key" in captured.err

    def test_duplicate_policy_first_keeps_first_record(self, write_jsonl, capsys):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}, {"id": 1, "ignored": True}])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        # Act
        exit_code = main(
            [str(old), str(new), "--key", "id", "--duplicates", "first", "--quiet"],
        )

        # Assert
        assert exit_code == 0
        assert capsys.readouterr() == ("", "")

    @pytest.mark.parametrize("stdin_side", ["old", "new"])
    def test_dash_reads_one_source_from_stdin(
        self,
        write_jsonl,
        capsys,
        monkeypatch,
        stdin_side,
    ):
        # Arrange
        source = write_jsonl("source.jsonl", [{"id": 1}])
        monkeypatch.setattr(
            jsonl_diff.sys,
            "stdin",
            io.TextIOWrapper(io.BytesIO(b'{"id":1}\n'), encoding="utf-8"),
        )
        arguments = (
            ["-", str(source), "--key", "id", "--quiet"]
            if stdin_side == "old"
            else [str(source), "-", "--key", "id", "--quiet"]
        )

        # Act
        exit_code = main(arguments)

        # Assert
        assert exit_code == 0
        assert capsys.readouterr() == ("", "")

    def test_both_sources_from_stdin_are_rejected(self, capsys):
        # Act
        with pytest.raises(SystemExit) as captured_exit:
            main(["-", "-", "--key", "id"])

        # Assert
        assert captured_exit.value.code == 3
        assert "cannot both read from stdin" in capsys.readouterr().err


class TestTextOutput:
    def test_summary_format_ok(self, write_jsonl, capsys):
        # Arrange
        old, new = _different_sources(write_jsonl)

        # Act
        exit_code = main([str(old), str(new), "--key", "id"])
        captured = capsys.readouterr()

        # Assert
        assert exit_code == 1
        assert captured.out == (
            "Records:\n"
            "  equal:     0\n"
            "  added:     1\n"
            "  deleted:   1\n"
            "  modified:  1\n"
        )

    def test_details_do_not_change_stdout_summary(self, write_jsonl, tmp_path, capsys):
        # Arrange
        old, new = _different_sources(write_jsonl)
        details = tmp_path / "changes.jsonl"

        # Act
        main([str(old), str(new), "--key", "id", "--details", str(details)])
        output = capsys.readouterr().out

        # Assert
        assert output == (
            "Records:\n"
            "  equal:     0\n"
            "  added:     1\n"
            "  deleted:   1\n"
            "  modified:  1\n"
        )


class TestDetailsOutput:
    def test_file_contains_versioned_metadata_and_summary(self, write_jsonl, tmp_path, capsys):
        # Arrange
        old = write_jsonl("old.jsonl", [{"country": "ES", "customer": 1, "volatile": "old"}])
        new = write_jsonl("new.jsonl", [{"country": "ES", "customer": 1, "volatile": "new"}])
        details = tmp_path / "changes.jsonl"

        # Act
        exit_code = main(
            [
                str(old),
                str(new),
                "--key",
                "country,customer",
                "--ignore",
                "/volatile",
                "--details",
                str(details),
                "--quiet",
            ],
        )
        records = [
            json.loads(line, parse_int=Decimal, parse_float=Decimal)
            for line in details.read_text(encoding="utf-8").splitlines()
        ]

        # Assert
        assert exit_code == 0
        assert not capsys.readouterr().out
        assert records == [{
            "type": "meta",
            "key": ["country", "customer"],
            "ignore": ["/volatile"],
            "duplicates": "error",
        }, {
            "type": "summary",
            "equal": 1,
            "added": 0,
            "deleted": 0,
            "modified": 0,
        }]

    def test_file_contains_deterministically_ordered_changes(
        self,
        write_jsonl,
        tmp_path,
        capsys,
    ):
        # Arrange
        old, new = _different_sources(write_jsonl)
        details = tmp_path / "changes.jsonl"

        # Act
        main([str(old), str(new), "--key", "id", "--details", str(details)])
        records = [json.loads(line) for line in details.read_text(encoding="utf-8").splitlines()]
        capsys.readouterr()

        # Assert
        assert records[1:-1] == [
            {
                "type": "change",
                "op": "deleted",
                "key": [1],
                "old_line": 1,
            },
            {
                "type": "change",
                "op": "modified",
                "key": [2],
                "old_line": 2,
                "new_line": 1,
            },
            {
                "type": "change",
                "op": "added",
                "key": [3],
                "new_line": 2,
            },
        ]

    def test_large_numeric_key_is_written_without_precision_loss(
        self,
        write_jsonl,
        tmp_path,
    ):
        # Arrange
        old = write_jsonl("old.jsonl", ['{"id":12345678901234567890}'])
        new = write_jsonl("new.jsonl", [])
        details = tmp_path / "changes.jsonl"

        # Act
        main([str(old), str(new), "--key", "id", "--details", str(details), "--quiet"])
        records = [
            json.loads(line, parse_int=Decimal, parse_float=Decimal)
            for line in details.read_text(encoding="utf-8").splitlines()
        ]

        # Assert
        assert records[1]["key"] == [12345678901234567890]


class TestQuietOutput:
    def test_quiet_suppresses_summary_but_writes_details(self, write_jsonl, tmp_path, capsys):
        # Arrange
        old, new = _different_sources(write_jsonl)
        details = tmp_path / "changes.jsonl"

        # Act
        exit_code = main(
            [
                str(old),
                str(new),
                "--key",
                "id",
                "--details",
                str(details),
                "--quiet",
            ],
        )
        captured = capsys.readouterr()

        # Assert
        assert exit_code == 1
        assert not captured.out
        assert details.is_file()
