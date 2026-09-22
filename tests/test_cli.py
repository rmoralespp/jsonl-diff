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

    def test_where_filters_records_before_comparison(self, write_jsonl, capsys):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [{"id": 1, "country": "ES", "value": "old"}, {"id": 2, "country": "FR", "value": "old"}],
        )
        new = write_jsonl(
            "new.jsonl",
            [{"id": 1, "country": "ES", "value": "new"}, {"id": 2, "country": "FR", "value": "new"}],
        )

        # Act
        exit_code = main(
            [str(old), str(new), "--key", "id", "--where", 'country == `"ES"`', "--quiet"],
        )

        # Assert: only id=1 (ES) participates, and it is modified.
        assert exit_code == 1
        assert capsys.readouterr() == ("", "")

    def test_invalid_where_expression_exits_with_three(self, write_jsonl, capsys):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        # Act
        with pytest.raises(SystemExit) as captured_exit:
            main([str(old), str(new), "--key", "id", "--where", "country =="])
        captured = capsys.readouterr()

        # Assert
        assert captured_exit.value.code == 3
        assert not captured.out

    def test_cli_and_python_api_agree_on_where_filtering(self, write_jsonl, capsys):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [{"id": 1, "active": True, "value": "old"}, {"id": 2, "active": False, "value": "old"}],
        )
        new = write_jsonl(
            "new.jsonl",
            [{"id": 1, "active": True, "value": "new"}, {"id": 2, "active": False, "value": "new"}],
        )

        # Act
        exit_code = main(
            [str(old), str(new), "--key", "id", "--where", "active == `true`", "--quiet"],
        )
        capsys.readouterr()
        with jsonl_diff.diff(old, new, key="id", where="active == `true`") as result:
            api_summary = result.summary

        # Assert
        assert (exit_code == 1) == api_summary.different
        assert api_summary == jsonl_diff.Summary(equal=0, added=0, deleted=0, modified=1)

    def test_duplicate_policy_first_keeps_first_record(self, write_jsonl, capsys):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}, {"id": 1, "ignored": True}])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        # Act
        exit_code = main(
            [str(old), str(new), "--key", "id", "--duplicates", "first", "--quiet"],
        )

        # Assert
        assert exit_code == 1
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

    def test_schema_ignore_without_schema_diff_is_rejected(self, write_jsonl, capsys):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        # Act
        with pytest.raises(SystemExit) as captured_exit:
            main([str(old), str(new), "--key", "id", "--schema-ignore", "/metadata"])

        # Assert
        assert captured_exit.value.code == 3
        assert "--schema-ignore requires --schema-diff" in capsys.readouterr().err


class TestKeyOptionParsing:
    def test_comma_separated_keys_with_surrounding_whitespace_are_trimmed(
        self,
        write_jsonl,
        capsys,
    ):
        # Arrange
        old = write_jsonl("old.jsonl", [{"country": "ES", "customer": 1, "value": "before"}])
        new = write_jsonl("new.jsonl", [{"country": "ES", "customer": 1, "value": "after"}])

        # Act
        exit_code = main(
            [str(old), str(new), "--key", " country , customer ", "--quiet"],
        )

        # Assert
        assert exit_code == 1
        assert capsys.readouterr() == ("", "")

    def test_empty_key_name_after_trimming_is_a_configuration_error(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        # Act
        with pytest.raises(SystemExit) as captured_exit:
            main([str(old), str(new), "--key", "id, "])

        # Assert
        assert captured_exit.value.code == 3

    def test_whitespace_only_key_name_is_a_configuration_error(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        # Act
        with pytest.raises(SystemExit) as captured_exit:
            main([str(old), str(new), "--key", "   "])

        # Assert
        assert captured_exit.value.code == 3


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
            "  OLD duplicates:  0\n"
            "  NEW duplicates:  0\n"
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
            "  OLD duplicates:  0\n"
            "  NEW duplicates:  0\n"
        )

    def test_tolerated_duplicates_are_reported_for_each_source(
        self,
        write_jsonl,
        capsys,
    ):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}, {"id": 1}])
        new = write_jsonl("new.jsonl", [{"id": 1}, {"id": 1}, {"id": 1}])

        # Act
        exit_code = main([str(old), str(new), "--key", "id", "--duplicates", "first"])
        output = capsys.readouterr().out

        # Assert
        assert exit_code == 1
        assert output.endswith(
            "  OLD duplicates:  1\n"
            "  NEW duplicates:  2\n",
        )

    def test_schema_diff_adds_observed_schema_summary(self, write_jsonl, capsys):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1, "legacy": "value"}])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        # Act
        exit_code = main(
            [
                str(old),
                str(new),
                "--key",
                "id",
                "--ignore",
                "/legacy",
                "--schema-diff",
            ],
        )
        output = capsys.readouterr().out

        # Assert
        assert exit_code == 1
        assert output == (
            "Records:\n"
            "  equal:     1\n"
            "  added:     0\n"
            "  deleted:   0\n"
            "  modified:  0\n"
            "  OLD duplicates:  0\n"
            "  NEW duplicates:  0\n"
            "\n"
            "Observed schema:\n"
            "  fields added:          0\n"
            "  fields removed:        1\n"
            "  type changes:          0\n"
            "  nullability changes:   0\n"
            "  requiredness changes:  0\n"
        )


class TestDetailsNumberOutput:
    def test_counts_and_lines_are_written_as_plain_integers(self, write_jsonl, tmp_path):
        # Arrange
        old, new = _different_sources(write_jsonl)
        details = tmp_path / "changes.jsonl"

        # Act
        main([str(old), str(new), "--key", "id", "--details", str(details), "--quiet"])
        lines = details.read_text(encoding="utf-8").splitlines()

        # Assert
        assert lines[1] == '{"key":[1],"old_line":1,"op":"deleted","type":"change"}'
        assert lines[-1] == (
            '{"added":1,"deleted":1,"equal":0,"modified":1,'
            '"new_duplicates":0,"old_duplicates":0,"type":"summary"}'
        )

    @pytest.mark.parametrize(
        "value",
        [
            "12345678901234567890",
            "0.12345678901234567890123456789",
        ],
    )
    def test_numeric_key_is_written_without_precision_loss(self, write_jsonl, tmp_path, value):
        # Arrange
        old = write_jsonl("old.jsonl", ['{"id":%s}' % value])
        new = write_jsonl("new.jsonl", [])
        details = tmp_path / "changes.jsonl"

        # Act
        main([str(old), str(new), "--key", "id", "--details", str(details), "--quiet"])
        line = details.read_text(encoding="utf-8").splitlines()[1]
        event = json.loads(line, parse_int=Decimal, parse_float=Decimal)

        # Assert
        assert event["key"] == [Decimal(value)]

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1e3", "1e+3"),
            ("1e50000000", "1e+50000000"),
            ("1e-50000000", "1e-50000000"),
        ],
    )
    def test_numeric_key_uses_compact_decimal_notation(
        self,
        write_jsonl,
        tmp_path,
        value,
        expected,
    ):
        # Arrange
        old = write_jsonl("old.jsonl", ['{"id":%s}' % value])
        new = write_jsonl("new.jsonl", [])
        details = tmp_path / "changes.jsonl"

        # Act
        main([str(old), str(new), "--key", "id", "--details", str(details), "--quiet"])
        line = details.read_text(encoding="utf-8").splitlines()[1]

        # Assert
        assert '"key":[{}]'.format(expected) in line

    def test_field_diff_values_use_readable_number_format(self, write_jsonl, tmp_path):
        # Arrange
        old = write_jsonl("old.jsonl", ['{"id":1,"value":12345678901234567890}'])
        new = write_jsonl("new.jsonl", ['{"id":1,"value":12345678901234567891}'])
        details = tmp_path / "changes.jsonl"

        # Act
        main(
            [
                str(old),
                str(new),
                "--key",
                "id",
                "--details",
                str(details),
                "--field-diff",
                "--quiet",
            ],
        )
        line = details.read_text(encoding="utf-8").splitlines()[1]

        # Assert
        assert '"old":12345678901234567890' in line
        assert '"new":12345678901234567891' in line


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
            "where": None,
            "duplicates": "error",
        }, {
            "type": "summary",
            "equal": 1,
            "added": 0,
            "deleted": 0,
            "modified": 0,
            "old_duplicates": 0,
            "new_duplicates": 0,
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

    def test_duplicate_events_follow_meta_and_include_policy_outcome(
        self,
        write_jsonl,
        tmp_path,
        capsys,
    ):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [
                {"id": 1, "value": "selected"},
                {"id": 1, "value": "selected"},
                {"id": 1, "value": "conflicting"},
            ],
        )
        new = write_jsonl("new.jsonl", [{"id": 1, "value": "selected"}])
        details = tmp_path / "changes.jsonl"

        # Act
        exit_code = main(
            [
                str(old),
                str(new),
                "--key",
                "id",
                "--duplicates",
                "first",
                "--details",
                str(details),
                "--quiet",
            ],
        )
        records = [json.loads(line) for line in details.read_text(encoding="utf-8").splitlines()]

        # Assert
        assert exit_code == 1
        assert capsys.readouterr() == ("", "")
        assert records[0]["duplicates"] == "first"
        assert records[1:3] == [
            {
                "type": "duplicate",
                "source": "OLD",
                "key": [1],
                "selected_line": 1,
                "discarded_line": 2,
                "content_equal": True,
            },
            {
                "type": "duplicate",
                "source": "OLD",
                "key": [1],
                "selected_line": 1,
                "discarded_line": 3,
                "content_equal": False,
            },
        ]
        assert records[-1]["old_duplicates"] == 2
        assert records[-1]["new_duplicates"] == 0

    def test_schema_events_follow_meta_and_precede_record_changes(
        self,
        write_jsonl,
        tmp_path,
        capsys,
    ):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [
                {"id": 1, "age": 36, "email": None},
                {"id": 2, "age": 41, "email": "alan@example.com"},
            ],
        )
        new = write_jsonl(
            "new.jsonl",
            [
                {"id": 1, "age": "36", "email": "ada@example.com", "country": "ES"},
                {"id": 2, "age": "41", "country": "UK"},
            ],
        )
        details = tmp_path / "changes.jsonl"

        # Act
        exit_code = main(
            [
                str(old),
                str(new),
                "--key",
                "id",
                "--schema-diff",
                "--details",
                str(details),
                "--quiet",
            ],
        )
        records = [json.loads(line) for line in details.read_text(encoding="utf-8").splitlines()]

        # Assert
        assert exit_code == 1
        assert capsys.readouterr() == ("", "")
        assert records[0]["schema_diff"] is True
        assert records[0]["schema_ignore"] == []
        assert [(event["op"], event["path"]) for event in records[1:5]] == [
            ("types_changed", "/age"),
            ("field_added", "/country"),
            ("nullability_changed", "/email"),
            ("requiredness_changed", "/email"),
        ]
        assert records[1]["old"]["types"] == {"integer": 2}
        assert records[1]["new"]["types"] == {"string": 2}
        assert [event["type"] for event in records[5:7]] == ["change", "change"]
        assert records[-1]["schema"] == {
            "fields_added": 1,
            "fields_removed": 0,
            "types_changed": 1,
            "nullability_changed": 1,
            "requiredness_changed": 1,
        }

    def test_field_diff_reports_nested_modified_values(self, write_jsonl, tmp_path, capsys):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [{
                "id": 123,
                "name": "John",
                "address": {"city": "Madrid"},
                "status": "pending",
            }],
        )
        new = write_jsonl(
            "new.jsonl",
            [{
                "id": 123,
                "name": "Jonathan",
                "address": {"city": "Barcelona"},
                "status": "approved",
            }],
        )
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
                "--field-diff",
                "--quiet",
            ],
        )
        records = [json.loads(line) for line in details.read_text(encoding="utf-8").splitlines()]
        captured = capsys.readouterr()

        # Assert
        assert exit_code == 1
        assert not captured.out
        assert records[0]["field_diff"] is True
        assert records[1]["changes"] == [
            {"path": "/address/city", "old": "Madrid", "new": "Barcelona"},
            {"path": "/name", "old": "John", "new": "Jonathan"},
            {"path": "/status", "old": "pending", "new": "approved"},
        ]

    def test_field_diff_distinguishes_missing_nulls_and_escapes_paths(
        self,
        write_jsonl,
        tmp_path,
        capsys,
    ):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [{"id": 1, "a/b": "old", "gone": None, "items": [1, 2]}],
        )
        new = write_jsonl(
            "new.jsonl",
            [{"id": 1, "a/b": "new", "added": None, "items": [1, 3, 4]}],
        )
        details = tmp_path / "changes.jsonl"

        # Act
        main(
            [
                str(old),
                str(new),
                "--key",
                "id",
                "--details",
                str(details),
                "--field-diff",
                "--quiet",
            ],
        )
        change = json.loads(details.read_text(encoding="utf-8").splitlines()[1])
        capsys.readouterr()

        # Assert
        assert change["changes"] == [
            {"path": "/a~1b", "old": "old", "new": "new"},
            {"path": "/added", "new": None},
            {"path": "/gone", "old": None},
            {"path": "/items/1", "old": 2, "new": 3},
            {"path": "/items/2", "new": 4},
        ]

    def test_field_diff_excludes_ignored_fields(self, write_jsonl, tmp_path, capsys):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1, "name": "old", "volatile": "old"}])
        new = write_jsonl("new.jsonl", [{"id": 1, "name": "new", "volatile": "new"}])
        details = tmp_path / "changes.jsonl"

        # Act
        main(
            [
                str(old),
                str(new),
                "--key",
                "id",
                "--ignore",
                "/volatile",
                "--details",
                str(details),
                "--field-diff",
                "--quiet",
            ],
        )
        change = json.loads(details.read_text(encoding="utf-8").splitlines()[1])
        capsys.readouterr()

        # Assert
        assert change["changes"] == [{"path": "/name", "old": "old", "new": "new"}]

    def test_field_diff_combines_with_schema_events(self, write_jsonl, tmp_path):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1, "age": 36}])
        new = write_jsonl("new.jsonl", [{"id": 1, "age": "36"}])
        details = tmp_path / "changes.jsonl"

        # Act
        main(
            [
                str(old),
                str(new),
                "--key",
                "id",
                "--schema-diff",
                "--field-diff",
                "--details",
                str(details),
                "--quiet",
            ],
        )
        records = [json.loads(line) for line in details.read_text(encoding="utf-8").splitlines()]

        # Assert
        assert records[0]["schema_diff"] is True
        assert records[0]["field_diff"] is True
        assert [event["type"] for event in records] == [
            "meta",
            "schema_change",
            "change",
            "summary",
        ]
        assert records[2]["changes"] == [{"path": "/age", "old": 36, "new": "36"}]

    @pytest.mark.parametrize(
        "policy,selected_line,discarded_line,selected_value",
        [
            ("first", 1, 2, "first"),
            ("last", 2, 1, "last"),
        ],
    )
    def test_field_diff_uses_record_selected_by_duplicate_policy(
        self,
        write_jsonl,
        tmp_path,
        policy,
        selected_line,
        discarded_line,
        selected_value,
    ):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [{"id": 1, "value": "first"}, {"id": 1, "value": "last"}],
        )
        new = write_jsonl("new.jsonl", [{"id": 1, "value": "new"}])
        details = tmp_path / "changes.jsonl"

        # Act
        main(
            [
                str(old),
                str(new),
                "--key",
                "id",
                "--duplicates",
                policy,
                "--field-diff",
                "--details",
                str(details),
                "--quiet",
            ],
        )
        records = [json.loads(line) for line in details.read_text(encoding="utf-8").splitlines()]

        # Assert
        assert records[1]["type"] == "duplicate"
        assert records[1]["selected_line"] == selected_line
        assert records[1]["discarded_line"] == discarded_line
        assert records[2]["old_line"] == selected_line
        assert records[2]["changes"] == [
            {"path": "/value", "old": selected_value, "new": "new"},
        ]


class TestFieldDiffArguments:
    def test_field_diff_requires_details(self, write_jsonl, capsys):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        # Act
        with pytest.raises(SystemExit) as captured_exit:
            main([str(old), str(new), "--key", "id", "--field-diff"])
        captured = capsys.readouterr()

        # Assert
        assert captured_exit.value.code == 3
        assert "--field-diff requires --details FILE" in captured.err

    def test_field_diff_rejects_stdin_source(self, write_jsonl, tmp_path, capsys):
        # Arrange
        new = write_jsonl("new.jsonl", [{"id": 1}])

        # Act
        with pytest.raises(SystemExit) as captured_exit:
            main(
                [
                    "-",
                    str(new),
                    "--key",
                    "id",
                    "--details",
                    str(tmp_path / "changes.jsonl"),
                    "--field-diff",
                ],
            )
        captured = capsys.readouterr()

        # Assert
        assert captured_exit.value.code == 3
        assert "--field-diff cannot be used with stdin" in captured.err


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
