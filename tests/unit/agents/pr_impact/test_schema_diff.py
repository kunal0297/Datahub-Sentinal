from sentinel.agents.pr_impact.schema_diff import diff_schemas
from sentinel.core.models import ColumnChangeType, SchemaField


def test_column_removed_is_breaking():
    before = [SchemaField(name="discount_pct", type="numeric")]
    after: list[SchemaField] = []
    changes = diff_schemas(before, after)
    assert len(changes) == 1
    assert changes[0].change_type == ColumnChangeType.COLUMN_REMOVED
    assert changes[0].breaking is True
    assert changes[0].column == "discount_pct"


def test_column_added_is_safe():
    before: list[SchemaField] = []
    after = [SchemaField(name="currency", type="varchar")]
    changes = diff_schemas(before, after)
    assert len(changes) == 1
    assert changes[0].change_type == ColumnChangeType.COLUMN_ADDED
    assert changes[0].breaking is False


def test_type_changed_is_breaking_by_default():
    before = [SchemaField(name="total_amount", type="varchar")]
    after = [SchemaField(name="total_amount", type="numeric")]
    changes = diff_schemas(before, after)
    assert len(changes) == 1
    assert changes[0].change_type == ColumnChangeType.TYPE_CHANGED
    assert changes[0].breaking is True
    assert changes[0].old_type == "varchar"
    assert changes[0].new_type == "numeric"


def test_type_changed_safe_widening_is_not_breaking():
    before = [SchemaField(name="order_count", type="int")]
    after = [SchemaField(name="order_count", type="bigint")]
    changes = diff_schemas(before, after)
    assert len(changes) == 1
    assert changes[0].change_type == ColumnChangeType.TYPE_CHANGED
    assert changes[0].breaking is False
    assert "safe widening" in changes[0].detail


def test_type_change_skipped_when_after_type_unknown():
    """File-parsed 'after' schemas often can't know a column's type (raw SQL
    SELECT lists don't declare types) -- diff_schemas must not guess."""
    before = [SchemaField(name="status", type="varchar")]
    after = [SchemaField(name="status", type=None)]
    changes = diff_schemas(before, after)
    assert changes == []


def test_unchanged_column_produces_no_change():
    before = [SchemaField(name="order_id", type="varchar")]
    after = [SchemaField(name="order_id", type="varchar")]
    assert diff_schemas(before, after) == []


def test_rename_without_hint_is_remove_plus_add():
    before = [SchemaField(name="discount_pct", type="numeric")]
    after = [SchemaField(name="discount_percentage", type="numeric")]
    changes = diff_schemas(before, after)
    types = {c.change_type for c in changes}
    assert types == {ColumnChangeType.COLUMN_REMOVED, ColumnChangeType.COLUMN_ADDED}
    assert len(changes) == 2


def test_rename_with_hint_is_single_renamed_change():
    before = [SchemaField(name="discount_pct", type="numeric")]
    after = [SchemaField(name="discount_percentage", type="numeric")]
    changes = diff_schemas(before, after, rename_hints={"discount_pct": "discount_percentage"})
    assert len(changes) == 1
    assert changes[0].change_type == ColumnChangeType.RENAMED
    assert changes[0].breaking is True
    assert changes[0].column == "discount_pct"
    assert changes[0].renamed_to == "discount_percentage"


def test_stale_rename_hint_is_ignored_not_raised():
    before = [SchemaField(name="discount_pct", type="numeric")]
    after = [SchemaField(name="discount_pct", type="numeric")]
    # hint references columns that don't exist on either side
    changes = diff_schemas(before, after, rename_hints={"old_ghost": "new_ghost"})
    assert changes == []


def test_orders_v1_to_v2_full_migration_diff():
    """Mirrors the real seed data migration (seed/seed_datahub.py) end to
    end: total_amount->total_amount_usd, discount_pct->discount_percentage,
    status->order_status (all renamed via hints), currency added."""
    before = [
        SchemaField(name="order_id", type="varchar"),
        SchemaField(name="customer_id", type="varchar"),
        SchemaField(name="order_date", type="timestamp"),
        SchemaField(name="total_amount", type="numeric"),
        SchemaField(name="discount_pct", type="numeric"),
        SchemaField(name="status", type="varchar"),
    ]
    after = [
        SchemaField(name="order_id", type="varchar"),
        SchemaField(name="customer_id", type="varchar"),
        SchemaField(name="order_date", type="timestamp"),
        SchemaField(name="total_amount_usd", type="numeric"),
        SchemaField(name="discount_percentage", type="numeric"),
        SchemaField(name="order_status", type="varchar"),
        SchemaField(name="currency", type="varchar"),
    ]
    hints = {
        "total_amount": "total_amount_usd",
        "discount_pct": "discount_percentage",
        "status": "order_status",
    }
    changes = diff_schemas(before, after, rename_hints=hints)
    by_column = {c.column: c for c in changes}

    assert len(changes) == 4
    assert by_column["total_amount"].change_type == ColumnChangeType.RENAMED
    assert by_column["discount_pct"].change_type == ColumnChangeType.RENAMED
    assert by_column["status"].change_type == ColumnChangeType.RENAMED
    assert by_column["currency"].change_type == ColumnChangeType.COLUMN_ADDED
    assert all(c.breaking for c in changes if c.change_type == ColumnChangeType.RENAMED)
    assert not by_column["currency"].breaking
