from sentinel.agents.migration_copilot.planner import build_column_mapping
from sentinel.core.models import SchemaField

OLD_URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v1,PROD)"
NEW_URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v2,PROD)"


def _fields(*names: str) -> list[SchemaField]:
    return [SchemaField(name=n) for n in names]


class TestExplicitMapping:
    def test_explicit_mapping_wins_over_exact_and_fuzzy(self):
        old = _fields("id")
        new = _fields("id", "identifier")
        # without a hint "id" would exact-match itself; force it onto "identifier" instead
        plan = build_column_mapping(
            OLD_URN, NEW_URN, old, new, explicit_mapping={"id": "identifier"}
        )
        assert len(plan.mappings) == 1
        assert plan.mappings[0].method == "explicit"
        assert plan.mappings[0].new_column == "identifier"
        assert plan.mappings[0].confidence == 1.0

    def test_explicit_hint_referencing_nonexistent_column_is_ignored(self):
        old = _fields("order_id")
        new = _fields("order_id")
        plan = build_column_mapping(
            OLD_URN, NEW_URN, old, new, explicit_mapping={"ghost_old": "ghost_new"}
        )
        # falls through to exact match on order_id; the bad hint is just ignored
        assert len(plan.mappings) == 1
        assert plan.mappings[0].method == "exact"


class TestExactMatch:
    def test_identical_names_match_exactly(self):
        old = _fields("order_id", "customer_id")
        new = _fields("order_id", "customer_id")
        plan = build_column_mapping(OLD_URN, NEW_URN, old, new)
        assert len(plan.mappings) == 2
        assert all(m.method == "exact" and m.confidence == 1.0 for m in plan.mappings)
        assert plan.unmapped_old_columns == []
        assert plan.unmapped_new_columns == []


class TestFuzzyMatch:
    def test_real_orders_v1_to_v2_migration_recovered_without_hints(self):
        """The actual seed/seed_datahub.py migration: no explicit hints given,
        fuzzy matching alone should recover the real mapping."""
        old = _fields(
            "order_id", "customer_id", "order_date", "total_amount", "discount_pct", "status"
        )
        new = _fields(
            "order_id",
            "customer_id",
            "order_date",
            "total_amount_usd",
            "discount_percentage",
            "order_status",
            "currency",
        )
        plan = build_column_mapping(OLD_URN, NEW_URN, old, new)
        by_old = {m.old_column: m for m in plan.mappings}

        assert by_old["order_id"].method == "exact"
        assert by_old["customer_id"].method == "exact"
        assert by_old["order_date"].method == "exact"
        assert by_old["total_amount"].new_column == "total_amount_usd"
        assert by_old["total_amount"].method == "fuzzy"
        assert by_old["discount_pct"].new_column == "discount_percentage"
        assert by_old["status"].new_column == "order_status"
        assert not any(m.ambiguous for m in plan.mappings)
        assert plan.unmapped_new_columns == ["currency"]
        assert plan.unmapped_old_columns == []

    def test_below_threshold_similarity_is_left_unmapped(self):
        old = _fields("a")
        new = _fields("completely_unrelated_name")
        plan = build_column_mapping(OLD_URN, NEW_URN, old, new)
        assert plan.mappings == []
        assert plan.unmapped_old_columns == ["a"]
        assert plan.unmapped_new_columns == ["completely_unrelated_name"]

    def test_ambiguous_candidates_are_flagged_for_human_review(self):
        """Two equally-plausible new columns for one old column must not be
        silently resolved -- this is the case the Definition of Done calls
        out by name."""
        old = _fields("amt")
        new = _fields("amt_a", "amt_b")
        plan = build_column_mapping(OLD_URN, NEW_URN, old, new)
        assert len(plan.mappings) == 1
        assert plan.mappings[0].ambiguous is True

    def test_as_column_mapping_dict_excludes_ambiguous_entries(self):
        old = _fields("amt")
        new = _fields("amt_a", "amt_b")
        plan = build_column_mapping(OLD_URN, NEW_URN, old, new)
        assert plan.as_column_mapping_dict() == {}

    def test_review_lines_flag_ambiguous_and_report_unmapped(self):
        old = _fields("amt", "zzz_nomatch_old")
        new = _fields("amt_a", "amt_b", "totally_different_new")
        plan = build_column_mapping(OLD_URN, NEW_URN, old, new)
        lines = "\n".join(plan.review_lines())
        assert "AMBIGUOUS" in lines
        assert "zzz_nomatch_old" in lines and "NO CONFIDENT MATCH" in lines
        assert "totally_different_new" in lines and "pure addition" in lines
