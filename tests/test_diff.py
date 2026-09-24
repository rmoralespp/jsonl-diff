from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from jsonl_diff import (
    ChangeOperation,
    ConfigurationError,
    DiffConfig,
    DuplicateKeyError,
    DuplicatePolicy,
    InputError,
    SchemaChangeOperation,
    SchemaSummary,
    Summary,
    diff,
)


class TestDiffSummary:
    def test_records_in_different_physical_order_are_classified_by_identity(self, write_jsonl):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [
                {"id": 1, "value": "same"},
                {"id": 2, "value": "deleted"},
                {"id": 3, "value": "before"},
            ],
        )
        new = write_jsonl(
            "new.jsonl",
            [
                {"id": 3, "value": "after"},
                {"id": 4, "value": "added"},
                {"value": "same", "id": 1},
            ],
        )

        # Act
        with diff(old, new, key="id") as result:
            summary = result.summary

        # Assert
        assert summary == Summary(equal=1, added=1, deleted=1, modified=1)

    def test_summary_properties_expose_classification(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}, {"id": 2}])
        new = write_jsonl("new.jsonl", [{"id": 2, "changed": True}, {"id": 3}])

        # Act
        with diff(old, new, key="id") as result:
            values = (result.equal, result.added, result.deleted, result.modified, result.different)

        # Assert
        assert values == (0, 1, 1, 1, True)

    def test_equal_inputs_report_not_different(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        # Act
        with diff(old, new, key="id") as result:
            different = result.different

        # Assert
        assert different is False


class TestIdentityKeys:
    def test_composite_key_is_returned_as_typed_tuple(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"country": "ES", "customer": 7, "value": "before"}])
        new = write_jsonl("new.jsonl", [{"country": "ES", "customer": 7, "value": "after"}])

        # Act
        with diff(old, new, key=("country", "customer")) as result:
            change = next(result.changes())

        # Assert
        assert change.key == ("ES", Decimal("7"))

    def test_composite_key_option_order_does_not_change_identity(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"a": 1, "b": 2, "value": "old"}])
        new = write_jsonl("new.jsonl", [{"a": 1, "b": 2, "value": "new"}])

        # Act
        with diff(old, new, key=("a", "b")) as first:
            first_change = next(first.changes())
        with diff(old, new, key=("b", "a")) as second:
            second_change = next(second.changes())

        # Assert
        assert first_change.key == second_change.key == (Decimal("1"), Decimal("2"))

    @pytest.mark.parametrize(
        "old_key,new_key",
        [
            ('"1"', "1"),
            ("true", "1"),
        ],
    )
    def test_identity_scalar_types_are_significant(self, write_jsonl, old_key, new_key):
        # Arrange
        old = write_jsonl("old.jsonl", ['{"id":%s}' % old_key])
        new = write_jsonl("new.jsonl", ['{"id":%s}' % new_key])

        # Act
        with diff(old, new, key="id") as result:
            summary = result.summary

        # Assert
        assert summary == Summary(equal=0, added=1, deleted=1, modified=0)

    @pytest.mark.parametrize(
        "old_number,new_number",
        [
            ("1", "1.0"),
            ("1.0", "1e0"),
            ("-0", "0.0"),
        ],
    )
    def test_numerically_equal_identity_and_content_are_equal(
        self,
        write_jsonl,
        old_number,
        new_number,
    ):
        # Arrange
        old = write_jsonl("old.jsonl", ['{"id":%s,"value":%s}' % (old_number, old_number)])
        new = write_jsonl("new.jsonl", ['{"id":%s,"value":%s}' % (new_number, new_number)])

        # Act
        with diff(old, new, key="id") as result:
            summary = result.summary

        # Assert
        assert summary == Summary(equal=1, added=0, deleted=0, modified=0)

    def test_extreme_exponents_remain_compact(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", ['{"id":1e50000000,"value":1e-50000000}'])
        new = write_jsonl("new.jsonl", ['{"id":1e50000000,"value":1e-50000000}'])

        # Act
        with diff(old, new, key="id") as result:
            summary = result.summary

        # Assert
        assert summary == Summary(equal=1, added=0, deleted=0, modified=0)


class TestContentComparison:
    def test_nested_object_member_order_is_ignored(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", ['{"id":1,"data":{"a":1,"b":2}}'])
        new = write_jsonl("new.jsonl", ['{"data":{"b":2,"a":1},"id":1}'])

        # Act
        with diff(old, new, key="id") as result:
            summary = result.summary

        # Assert
        assert summary == Summary(equal=1, added=0, deleted=0, modified=0)

    def test_array_member_order_is_significant(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1, "items": [1, 2, 3]}])
        new = write_jsonl("new.jsonl", [{"id": 1, "items": [3, 2, 1]}])

        # Act
        with diff(old, new, key="id") as result:
            summary = result.summary

        # Assert
        assert summary == Summary(equal=0, added=0, deleted=0, modified=1)


class TestIgnoredFields:
    def test_exact_pointer_does_not_ignore_same_name_at_another_depth(self, write_jsonl):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [{"id": 1, "updated_at": "old", "metadata": {"updated_at": "old"}}],
        )
        new = write_jsonl(
            "new.jsonl",
            [{"id": 1, "updated_at": "new", "metadata": {"updated_at": "new"}}],
        )

        # Act
        with diff(old, new, key="id", ignore=("/updated_at",)) as result:
            summary = result.summary

        # Assert
        assert summary.modified == 1

    @pytest.mark.parametrize(
        "pointer,field",
        [
            ("/a~1b", "a/b"),
            ("/tilde~0name", "tilde~name"),
        ],
    )
    def test_pointer_escapes_select_exact_object_member(self, write_jsonl, pointer, field):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1, field: "old"}])
        new = write_jsonl("new.jsonl", [{"id": 1, field: "new"}])

        # Act
        with diff(old, new, key="id", ignore=(pointer,)) as result:
            summary = result.summary

        # Assert
        assert summary.equal == 1

    def test_absent_pointer_is_not_an_error(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}])
        new = write_jsonl("new.jsonl", [{"id": 1, "volatile": "new"}])

        # Act
        with diff(old, new, key="id", ignore=("/volatile",)) as result:
            summary = result.summary

        # Assert
        assert summary.equal == 1

    def test_pointer_traversing_array_is_rejected(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1, "items": [{"volatile": "old"}]}])
        new = write_jsonl("new.jsonl", [{"id": 1, "items": [{"volatile": "new"}]}])

        # Act / Assert
        with pytest.raises(InputError, match="ignore paths may not traverse arrays"):
            with diff(old, new, key="id", ignore=("/items/0/volatile",)):
                pass

    def test_ignored_identity_field_is_rejected(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        # Act / Assert
        with pytest.raises(ConfigurationError, match="identity field 'id' cannot be ignored"):
            diff(old, new, key="id", ignore=("/id",))

    @pytest.mark.parametrize("pointer", ["updated_at", "/bad~escape"])
    def test_invalid_pointer_is_rejected(self, write_jsonl, pointer):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        # Act / Assert
        with pytest.raises(ConfigurationError):
            diff(old, new, key="id", ignore=(pointer,))


class TestWhereFiltering:
    def test_where_omitted_behaves_like_before(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1, "country": "ES"}, {"id": 2, "country": "FR"}])
        new = write_jsonl("new.jsonl", [{"id": 1, "country": "ES"}, {"id": 2, "country": "FR"}])

        # Act
        with diff(old, new, key="id") as result:
            summary = result.summary

        # Assert
        assert result.config.where is None
        assert summary == Summary(equal=2, added=0, deleted=0, modified=0)

    def test_where_selects_matching_records_only(self, write_jsonl):
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
        with diff(old, new, key="id", where='country == `"ES"`') as result:
            summary = result.summary

        # Assert: id=2 (FR) never enters the comparison at all.
        assert summary == Summary(equal=0, added=0, deleted=0, modified=1)

    def test_where_is_applied_independently_per_side(self, write_jsonl):
        # Arrange: id=2 only matches the filter in NEW, so it must appear as
        # an addition rather than modified/equal.
        old = write_jsonl(
            "old.jsonl",
            [{"id": 1, "active": True}, {"id": 2, "active": False}],
        )
        new = write_jsonl(
            "new.jsonl",
            [{"id": 1, "active": True}, {"id": 2, "active": True}],
        )

        # Act
        with diff(old, new, key="id", where="active == `true`") as result:
            summary = result.summary
            changes = list(result.changes())

        # Assert
        assert summary == Summary(equal=1, added=1, deleted=0, modified=0)
        assert changes[0].operation == ChangeOperation.ADDED
        assert changes[0].key == (Decimal("2"),)

    def test_filtered_records_are_not_reported_as_changes(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1, "active": False, "value": "old"}])
        new = write_jsonl("new.jsonl", [{"id": 1, "active": False, "value": "new"}])

        # Act
        with diff(old, new, key="id", where="active == `true`") as result:
            summary = result.summary
            changes = list(result.changes())

        # Assert
        assert summary == Summary(equal=0, added=0, deleted=0, modified=0)
        assert changes == []

    def test_filtered_records_do_not_trigger_duplicate_detection(self, write_jsonl):
        # Arrange: two records share id=1 but only one passes the filter.
        old = write_jsonl(
            "old.jsonl",
            [{"id": 1, "active": True}, {"id": 1, "active": False}],
        )
        new = write_jsonl("new.jsonl", [{"id": 1, "active": True}])

        # Act
        with diff(old, new, key="id", where="active == `true`") as result:
            summary = result.summary

        # Assert
        assert summary == Summary(equal=1, added=0, deleted=0, modified=0)

    def test_where_runtime_type_error_is_reported_as_input_error(self, write_jsonl):
        # Arrange: sum() requires an array of numbers; "values" holds strings.
        old = write_jsonl("old.jsonl", [{"id": 1, "values": ["a", "b"]}])
        new = write_jsonl("new.jsonl", [])

        # Act / Assert
        with pytest.raises(InputError, match="OLD at line 1"):
            with diff(old, new, key="id", where="sum(values) > `0`"):
                pass

    def test_where_supports_nested_field_expressions(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1, "meta": {"active": True}, "value": "old"}])
        new = write_jsonl("new.jsonl", [{"id": 1, "meta": {"active": True}, "value": "new"}])
        skipped_old = write_jsonl("old_skip.jsonl", [{"id": 1, "meta": {"active": False}}])
        skipped_new = write_jsonl("new_skip.jsonl", [{"id": 1, "meta": {"active": False}}])

        # Act
        with diff(old, new, key="id", where="meta.active == `true`") as matched:
            matched_summary = matched.summary
        with diff(skipped_old, skipped_new, key="id", where="meta.active == `true`") as skipped:
            skipped_summary = skipped.summary

        # Assert
        assert matched_summary == Summary(equal=0, added=0, deleted=0, modified=1)
        assert skipped_summary == Summary(equal=0, added=0, deleted=0, modified=0)

    def test_where_supports_array_expressions(self, write_jsonl):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [{"id": 1, "tags": ["a", "b"], "value": "old"}, {"id": 2, "tags": ["c"], "value": "old"}],
        )
        new = write_jsonl(
            "new.jsonl",
            [{"id": 1, "tags": ["a", "b"], "value": "new"}, {"id": 2, "tags": ["c"], "value": "new"}],
        )

        # Act
        with diff(old, new, key="id", where='contains(tags, `"a"`)') as result:
            summary = result.summary

        # Assert
        assert summary == Summary(equal=0, added=0, deleted=0, modified=1)

    def test_where_filtering_out_all_records_is_not_an_error(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1, "active": False}])
        new = write_jsonl("new.jsonl", [{"id": 2, "active": False}])

        # Act
        with diff(old, new, key="id", where="active == `true`") as result:
            summary = result.summary

        # Assert
        assert summary == Summary(equal=0, added=0, deleted=0, modified=0)

    def test_invalid_where_expression_is_a_configuration_error(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        # Act / Assert
        with pytest.raises(ConfigurationError):
            diff(old, new, key="id", where="country ==")

    def test_where_combined_with_composite_key(self, write_jsonl):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [{"a": 1, "b": 2, "country": "ES", "value": "old"}, {"a": 3, "b": 4, "country": "FR", "value": "old"}],
        )
        new = write_jsonl(
            "new.jsonl",
            [{"a": 1, "b": 2, "country": "ES", "value": "new"}, {"a": 3, "b": 4, "country": "FR", "value": "new"}],
        )

        # Act
        with diff(old, new, key=("a", "b"), where='country == `"ES"`') as result:
            summary = result.summary

        # Assert
        assert summary == Summary(equal=0, added=0, deleted=0, modified=1)

    def test_where_combined_with_ignore(self, write_jsonl):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [{"id": 1, "country": "ES", "updated_at": "t0", "value": "same"}],
        )
        new = write_jsonl(
            "new.jsonl",
            [{"id": 1, "country": "ES", "updated_at": "t1", "value": "same"}],
        )

        # Act
        with diff(old, new, key="id", where='country == `"ES"`', ignore=("/updated_at",)) as result:
            summary = result.summary

        # Assert
        assert summary == Summary(equal=1, added=0, deleted=0, modified=0)

    def test_where_expression_is_compiled_once_and_reused(self, write_jsonl, monkeypatch):
        # Arrange
        import jmespath

        import jsonl_diff

        old = write_jsonl("old.jsonl", [{"id": 1, "active": True}, {"id": 2, "active": True}])
        new = write_jsonl("new.jsonl", [{"id": 1, "active": True}, {"id": 2, "active": True}])
        calls = []
        original_compile = jmespath.compile

        def counting_compile(expression):
            calls.append(expression)
            return original_compile(expression)

        monkeypatch.setattr(jsonl_diff.jmespath, "compile", counting_compile)

        # Act
        with diff(old, new, key="id", where="active == `true`") as result:
            summary = result.summary

        # Assert: compiled once during eager configuration validation and reused
        # for every record (4 records total).
        assert summary == Summary(equal=2, added=0, deleted=0, modified=0)
        assert calls == ["active == `true`"]


class TestInputValidation:
    @pytest.mark.parametrize(
        "record",
        [
            '{"id":1',
            '{"id":NaN}',
            '{"id":Infinity}',
            '{"id":-Infinity}',
        ],
    )
    def test_invalid_json_is_rejected(self, write_jsonl, record):
        # Arrange
        old = write_jsonl("old.jsonl", [record])
        new = write_jsonl("new.jsonl", [])

        # Act / Assert
        with pytest.raises(InputError):
            with diff(old, new, key="id"):
                pass

    def test_duplicate_object_keys_follow_last_wins(self, write_jsonl):
        # Arrange: a repeated property keeps the last occurrence, so both records
        # collapse to {"id": 2} and compare equal.
        old = write_jsonl("old.jsonl", ['{"id":1,"id":2}'])
        new = write_jsonl("new.jsonl", ['{"id":2}'])

        # Act
        with diff(old, new, key="id") as result:
            summary = result.summary

        # Assert
        assert summary == Summary(equal=1, added=0, deleted=0, modified=0)

    @pytest.mark.parametrize("record", ["[]", '"record"', "1", "true", "null"])
    def test_non_object_record_is_rejected(self, write_jsonl, record):
        # Arrange
        old = write_jsonl("old.jsonl", [record])
        new = write_jsonl("new.jsonl", [])

        # Act / Assert
        with pytest.raises(InputError, match="each record must be a JSON object"):
            with diff(old, new, key="id"):
                pass

    @pytest.mark.parametrize(
        "record,message",
        [
            ({}, "missing identity field"),
            ({"id": None}, "must be a non-null scalar"),
            ({"id": []}, "must be a non-null scalar"),
            ({"id": {}}, "must be a non-null scalar"),
        ],
    )
    def test_invalid_identity_component_is_rejected(self, write_jsonl, record, message):
        # Arrange
        old = write_jsonl("old.jsonl", [record])
        new = write_jsonl("new.jsonl", [])

        # Act / Assert
        with pytest.raises(InputError, match=message):
            with diff(old, new, key="id"):
                pass

    def test_null_component_is_accepted_in_composite_identity(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"a": 1, "b": None, "value": "old"}])
        new = write_jsonl("new.jsonl", [{"a": 1, "b": None, "value": "new"}])

        # Act
        with diff(old, new, key=("a", "b")) as result:
            change = next(result.changes())

        # Assert
        assert change.key == (Decimal("1"), None)
        assert change.operation == ChangeOperation.MODIFIED

    def test_null_component_still_distinguishes_composite_identities(self, write_jsonl):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [{"a": 1, "b": None, "value": "one"}, {"a": 2, "b": None, "value": "two"}],
        )
        new = write_jsonl(
            "new.jsonl",
            [{"a": 1, "b": None, "value": "one"}, {"a": 2, "b": None, "value": "changed"}],
        )

        # Act
        with diff(old, new, key=("a", "b")) as result:
            summary = result.summary

        # Assert
        assert summary == Summary(added=0, deleted=0, equal=1, modified=1)

    def test_duplicate_identity_reports_source_and_physical_lines(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}, {"id": 2}, {"id": 1}])
        new = write_jsonl("new.jsonl", [])

        # Act
        with pytest.raises(DuplicateKeyError) as captured:
            with diff(old, new, key="id"):
                pass

        # Assert
        assert captured.value.lines == (1, 3)
        assert captured.value.source == "OLD"

    def test_duplicate_identity_is_detected_across_insert_batches(self, write_jsonl):
        # A duplicate whose two occurrences are separated by more than one
        # insert batch must still report the exact first and duplicate physical
        # lines, exercising the batched-insert cleanup-and-replay path.
        import jsonl_diff

        span = jsonl_diff._INSERT_BATCH + 500
        records = [{"id": index + 100_000} for index in range(span)]
        records[-1] = {"id": 100_000}
        old = write_jsonl("old.jsonl", records)
        new = write_jsonl("new.jsonl", [])

        # Act
        with pytest.raises(DuplicateKeyError) as captured:
            with diff(old, new, key="id"):
                pass

        # Assert
        assert captured.value.key == (Decimal(100_000),)
        assert captured.value.lines == (1, span)
        assert captured.value.source == "OLD"

    @pytest.mark.parametrize(
        "policy,expected",
        [
            (
                DuplicatePolicy.FIRST,
                Summary(equal=1, added=0, deleted=0, modified=0, old_duplicates=1),
            ),
            (
                DuplicatePolicy.LAST,
                Summary(equal=0, added=0, deleted=0, modified=1, old_duplicates=1),
            ),
        ],
    )
    def test_duplicate_policy_selects_record(self, write_jsonl, policy, expected):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [{"id": 1, "value": "first"}, {"id": 1, "value": "last"}],
        )
        new = write_jsonl("new.jsonl", [{"id": 1, "value": "first"}])

        # Act
        with diff(old, new, key="id", duplicates=policy) as result:
            summary = result.summary

        # Assert
        assert summary == expected

    @pytest.mark.parametrize(
        "policy,selected_line,discarded",
        [
            (DuplicatePolicy.FIRST, 1, [(2, False), (3, True)]),
            (DuplicatePolicy.LAST, 3, [(1, True), (2, False)]),
        ],
    )
    def test_tolerated_duplicates_report_selected_and_discarded_occurrences(
        self,
        write_jsonl,
        policy,
        selected_line,
        discarded,
    ):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [
                {"id": 1, "value": "selected"},
                {"id": 1, "value": "conflicting"},
                {"id": 1, "value": "selected"},
            ],
        )
        new = write_jsonl("new.jsonl", [{"id": 1, "value": "selected"}])

        # Act
        with diff(old, new, key="id", duplicates=policy) as result:
            summary = result.summary
            duplicates = list(result.duplicates())

        # Assert
        assert summary == Summary(
            equal=1,
            added=0,
            deleted=0,
            modified=0,
            old_duplicates=2,
        )
        assert summary.different is False
        assert summary.has_duplicates is True
        assert summary.has_issues is True
        assert [
            (
                duplicate.source,
                duplicate.key,
                duplicate.selected_line,
                duplicate.discarded_line,
                duplicate.content_equal,
            )
            for duplicate in duplicates
        ] == [
            ("OLD", (Decimal("1"),), selected_line, line, content_equal)
            for line, content_equal in discarded
        ]

    def test_duplicate_counts_are_extra_occurrences_on_each_side(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}, {"id": 1}])
        new = write_jsonl("new.jsonl", [{"id": 1}, {"id": 1}, {"id": 1}])

        # Act
        with diff(old, new, key="id", duplicates="first") as result:
            summary = result.summary
            sources = [duplicate.source for duplicate in result.duplicates()]

        # Assert
        assert summary.old_duplicates == 1
        assert summary.new_duplicates == 2
        assert sources == ["OLD", "NEW", "NEW"]

    def test_duplicate_content_equality_uses_ignored_content(self, write_jsonl):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [
                {"id": 1, "value": "same", "metadata": "first"},
                {"id": 1, "value": "same", "metadata": "last"},
            ],
        )
        new = write_jsonl("new.jsonl", [{"id": 1, "value": "same"}])

        # Act
        with diff(old, new, key="id", ignore=("/metadata",), duplicates="first") as result:
            duplicate = next(result.duplicates())

        # Assert
        assert duplicate.content_equal is True

    def test_invalid_duplicate_policy_is_rejected(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [])
        new = write_jsonl("new.jsonl", [])

        # Act / Assert
        with pytest.raises(ConfigurationError, match="invalid duplicate policy"):
            diff(old, new, key="id", duplicates="unknown")

    def test_malformed_record_reports_physical_line(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}, '{"id":'])
        new = write_jsonl("new.jsonl", [])

        # Act
        with pytest.raises(InputError) as captured:
            with diff(old, new, key="id"):
                pass

        # Assert
        assert captured.value.line == 2

    def test_blank_line_is_rejected_at_its_physical_line(self, tmp_path, write_jsonl):
        # Arrange
        old = tmp_path / "old.jsonl"
        old.write_text('{"id":1}\n\n{"id":2}\n', encoding="utf-8")
        new = write_jsonl("new.jsonl", [])

        # Act
        with pytest.raises(InputError) as captured:
            with diff(old, new, key="id"):
                pass

        # Assert
        assert captured.value.line == 2


class TestSchemaDiff:
    def test_diff_config_preserves_positional_where_expression(self):
        # Arrange
        expression = object()

        # Act
        config = DiffConfig(
            ("id",),
            (),
            None,
            DuplicatePolicy.ERROR,
            None,
            expression,
        )

        # Assert
        assert config.where_expression is expression
        assert config.schema_diff is False
        assert config.schema_ignore == ()

    def test_reports_field_type_nullability_and_requiredness_changes(self, write_jsonl):
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

        # Act
        with diff(old, new, key="id", schema_diff=True) as result:
            summary = result.summary
            changes = list(result.schema_changes())

        # Assert
        assert summary.schema == SchemaSummary(
            fields_added=1,
            types_changed=1,
            nullability_changed=1,
            requiredness_changed=1,
        )
        assert summary.has_schema_changes is True
        assert summary.has_issues is True
        assert [(change.operation, change.path) for change in changes] == [
            (SchemaChangeOperation.TYPES_CHANGED, "/age"),
            (SchemaChangeOperation.FIELD_ADDED, "/country"),
            (SchemaChangeOperation.NULLABILITY_CHANGED, "/email"),
            (SchemaChangeOperation.REQUIREDNESS_CHANGED, "/email"),
        ]
        email = changes[-1]
        assert email.old.parent_objects == 2
        assert email.old.present == 2
        assert email.old.nulls == 1
        assert email.old.type_counts == {"string": 1}
        assert email.new.missing == 1

    def test_nested_objects_use_escaped_pointers_and_arrays_are_terminal(self, write_jsonl):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [{"id": 1, "meta": {"a/b": 1}, "items": [{"value": 1}]}],
        )
        new = write_jsonl(
            "new.jsonl",
            [{"id": 1, "meta": {"a/b": "1"}, "items": [{"other": True}]}],
        )

        # Act
        with diff(old, new, key="id", schema_diff=True) as result:
            changes = list(result.schema_changes())

        # Assert
        assert [(change.operation, change.path) for change in changes] == [
            (SchemaChangeOperation.TYPES_CHANGED, "/meta/a~1b"),
        ]
        assert all(not change.path.startswith("/items/") for change in changes)

    def test_nested_requiredness_is_relative_to_parent_objects(self, write_jsonl):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [{"id": 1, "profile": {"city": "Madrid"}}, {"id": 2}],
        )
        new = write_jsonl(
            "new.jsonl",
            [{"id": 1, "profile": {"city": "Madrid"}}, {"id": 2, "profile": {}}],
        )

        # Act
        with diff(old, new, key="id", schema_diff=True) as result:
            changes = list(result.schema_changes(SchemaChangeOperation.REQUIREDNESS_CHANGED))

        # Assert
        city = next(change for change in changes if change.path == "/profile/city")
        assert city.old.parent_objects == 1
        assert city.old.required is True
        assert city.new.parent_objects == 2
        assert city.new.required is False

    def test_schema_ignore_is_independent_from_content_ignore(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1, "legacy": "value"}])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        # Act
        with diff(old, new, key="id", ignore=("/legacy",), schema_diff=True) as observed:
            observed_summary = observed.summary
        with diff(
            old,
            new,
            key="id",
            ignore=("/legacy",),
            schema_diff=True,
            schema_ignore=("/legacy",),
        ) as ignored:
            ignored_summary = ignored.summary

        # Assert
        assert observed_summary.different is False
        assert observed_summary.has_schema_changes is True
        assert ignored_summary.has_issues is False

    def test_schema_profile_includes_discarded_duplicate_occurrences(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}, {"id": 1, "extra": True}])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        # Act
        with diff(old, new, key="id", duplicates="first", schema_diff=True) as result:
            changes = list(result.schema_changes())
            summary = result.summary

        # Assert
        assert summary.different is False
        assert summary.old_duplicates == 1
        assert [(change.operation, change.path) for change in changes] == [
            (SchemaChangeOperation.FIELD_REMOVED, "/extra"),
        ]

    def test_schema_counts_are_aggregated_across_batches(self, write_jsonl):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [{"id": index, "value": index} for index in range(1025)],
        )
        new = write_jsonl(
            "new.jsonl",
            [{"id": index, "value": str(index)} for index in range(1025)],
        )

        # Act
        with diff(old, new, key="id", schema_diff=True) as result:
            change = next(
                result.schema_changes(SchemaChangeOperation.TYPES_CHANGED),
            )

        # Assert
        assert change.path == "/value"
        assert change.old.type_counts == {"integer": 1025}
        assert change.new.type_counts == {"string": 1025}

    def test_schema_types_follow_json_value_semantics(self, write_jsonl):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [
                {"id": 1, "value": True},
                {"id": 2, "value": 1.0},
                {"id": 3, "value": 1.5},
            ],
        )
        new = write_jsonl(
            "new.jsonl",
            [
                {"id": 1, "value": "true"},
                {"id": 2, "value": "1.0"},
                {"id": 3, "value": "1.5"},
            ],
        )

        # Act
        with diff(old, new, key="id", schema_diff=True) as result:
            change = next(
                result.schema_changes(SchemaChangeOperation.TYPES_CHANGED),
            )

        # Assert
        assert change.old.type_counts == {
            "boolean": 1,
            "integer": 1,
            "number": 1,
        }
        assert change.new.type_counts == {"string": 3}

    def test_schema_profile_only_includes_records_selected_by_where(self, write_jsonl):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [
                {"id": 1, "active": True, "legacy": "selected"},
                {"id": 2, "active": False, "ignored_by_where": "old"},
            ],
        )
        new = write_jsonl(
            "new.jsonl",
            [
                {"id": 1, "active": True},
                {"id": 2, "active": False, "new_but_ignored": "new"},
            ],
        )

        # Act
        with diff(
            old,
            new,
            key="id",
            where="active == `true`",
            schema_diff=True,
        ) as result:
            changes = list(result.schema_changes())

        # Assert
        assert [(change.operation, change.path) for change in changes] == [
            (SchemaChangeOperation.FIELD_REMOVED, "/legacy"),
        ]

    def test_schema_ignore_requires_schema_diff(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [])
        new = write_jsonl("new.jsonl", [])

        # Act / Assert
        with pytest.raises(ConfigurationError, match="schema_ignore requires schema_diff"):
            diff(old, new, key="id", schema_ignore=("/metadata",))

    def test_schema_iteration_requires_enabled_open_result(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        # Act / Assert
        with diff(old, new, key="id") as result:
            with pytest.raises(RuntimeError, match="schema diff was not enabled"):
                next(result.schema_changes())


class TestChanges:
    def test_changed_records_include_original_physical_lines(self, write_jsonl):
        # Arrange
        old = write_jsonl(
            "old.jsonl",
            [{"id": 9}, {"id": 2, "value": "old"}, {"id": 1}],
        )
        new = write_jsonl(
            "new.jsonl",
            [{"id": 3}, {"id": 9}, {"id": 2, "value": "new"}],
        )

        # Act
        with diff(old, new, key="id") as result:
            changes = {
                change.operation: (change.old_line, change.new_line)
                for change in result.changes()
            }

        # Assert
        assert changes == {
            ChangeOperation.ADDED: (None, 1),
            ChangeOperation.DELETED: (3, None),
            ChangeOperation.MODIFIED: (2, 3),
        }

    def test_changes_can_be_filtered_by_operation(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [{"id": 1}, {"id": 2}])
        new = write_jsonl("new.jsonl", [{"id": 2, "changed": True}, {"id": 3}])

        # Act
        with diff(old, new, key="id") as result:
            added = list(result.changes(ChangeOperation.ADDED))

        # Assert
        assert [(change.operation, change.key) for change in added] == [
            (ChangeOperation.ADDED, (Decimal("3"),)),
        ]

    def test_changes_have_stable_canonical_order(self, write_jsonl):
        # Arrange
        records = [
            '{"id":"a"}',
            '{"id":100}',
            '{"id":true}',
            '{"id":2}',
            '{"id":false}',
            '{"id":10}',
        ]
        first = write_jsonl("first.jsonl", records)
        second = write_jsonl("second.jsonl", reversed(records))
        new = write_jsonl("new.jsonl", [])

        # Act
        with diff(first, new, key="id") as result:
            first_keys = [change.key for change in result.changes()]
        with diff(second, new, key="id") as result:
            second_keys = [change.key for change in result.changes()]

        # Assert
        assert first_keys == second_keys

    def test_changes_returns_lazy_iterator(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [])
        new = write_jsonl("new.jsonl", [{"id": 1}])

        # Act
        with diff(old, new, key="id") as result:
            changes = result.changes()

            # Assert
            assert isinstance(changes, Iterator)

    def test_closed_result_rejects_change_iteration(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [])
        new = write_jsonl("new.jsonl", [{"id": 1}])
        result = diff(old, new, key="id")
        result.close()

        # Act / Assert
        with pytest.raises(RuntimeError, match="diff result is closed"):
            next(result.changes())

    def test_context_manager_cleans_disk_backed_workspace(self, tmp_path, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [])
        new = write_jsonl("new.jsonl", [{"id": 1}])
        result = diff(old, new, key="id")

        # Act
        with result:
            workspace = result._workspace.name
            exists_while_open = Path(workspace).is_dir()

        # Assert
        assert exists_while_open
        assert not Path(workspace).exists()

    def test_result_before_enter_rejects_summary_access(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [])
        new = write_jsonl("new.jsonl", [])
        result = diff(old, new, key="id")

        # Act / Assert
        with pytest.raises(RuntimeError, match="must be used as a context manager"):
            bool(result.equal)

    def test_result_cannot_be_entered_twice(self, write_jsonl):
        # Arrange
        old = write_jsonl("old.jsonl", [])
        new = write_jsonl("new.jsonl", [])
        result = diff(old, new, key="id")
        with result:
            pass

        # Act / Assert
        with pytest.raises(RuntimeError, match="cannot be entered more than once"):
            with result:
                pass
