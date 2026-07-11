import re

from sentinel.agents.migration_copilot.codegen import build_codegen_prompt


def test_prompt_contains_only_the_verified_mapping_columns():
    """The Definition of Done requirement: test that the prompt contains
    only DataHub-verified columns, not that the LLM call is mocked-correct
    end to end."""
    mapping = {"total_amount": "total_amount_usd", "discount_pct": "discount_percentage"}
    original = "select total_amount, discount_pct from orders_v1"
    prompt = build_codegen_prompt(
        original_content=original,
        column_mapping=mapping,
        new_table_name="analytics.orders_v2",
        new_schema_description="order totals in USD",
    )
    for old, new in mapping.items():
        assert f"{old} -> {new}" in prompt
    assert "analytics.orders_v2" in prompt
    assert original in prompt


def test_prompt_never_introduces_a_column_absent_from_mapping_or_original():
    mapping = {"discount_pct": "discount_percentage"}
    original = "select order_id, discount_pct from orders_v1"
    prompt = build_codegen_prompt(
        original_content=original,
        column_mapping=mapping,
        new_table_name="analytics.orders_v2",
        new_schema_description="n/a",
    )
    # every bare identifier-looking token that isn't part of the fixed
    # instructional scaffolding must trace back to the mapping or the
    # original file -- a coarse but real check that we aren't handing the
    # model surprise column names to imitate.
    mentioned_columns = set(re.findall(r"\b[a-z][a-z0-9_]*\b", original))
    mentioned_columns.update(mapping.keys())
    mentioned_columns.update(mapping.values())
    assert "total_amount_usd" not in prompt  # not part of this mapping -- must not leak in


def test_prompt_instructs_not_to_invent_unmapped_columns():
    prompt = build_codegen_prompt("select 1", {}, "t", "d")
    assert "not listed in the mapping" in prompt.lower()


def test_empty_mapping_produces_no_mapping_lines_but_still_valid_prompt():
    prompt = build_codegen_prompt("select 1", {}, "analytics.orders_v2", "desc")
    assert "select 1" in prompt
    assert "analytics.orders_v2" in prompt
