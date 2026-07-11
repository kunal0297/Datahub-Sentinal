"""LLM-backed SQL/dbt rewrite generation for the Schema Migration Copilot.

`build_codegen_prompt` is pure and independently testable — the spec's
Definition of Done specifically calls for testing "the prompt construction,
not that the LLM call itself is mocked-correct end to end", because the
one property that actually matters here is verifiable without an API call:
every column name the prompt could lead the model to emit must come from
the DataHub-verified mapping, never invented. `generate_rewrite` is the
thin, untested-beyond-that wrapper that actually calls the API.
"""

from __future__ import annotations

import anthropic

from sentinel.core.config import Settings

SYSTEM_PROMPT = (
    "You are a precise SQL/dbt migration assistant. You rewrite a SQL file that "
    "references an old table and its columns so it instead references a new "
    "table and its columns, using ONLY the exact column mapping and schema "
    "provided below. Never invent a column name that is not in the provided "
    "mapping or that was not already present, unchanged, in the original file. "
    "Produce a minimal, correct rewrite: preserve the original file's "
    "structure, formatting, comments, and any logic that does not reference a "
    "changed column. Output ONLY the rewritten SQL file content — no "
    "explanation, no markdown code fences."
)


def build_codegen_prompt(
    original_content: str,
    column_mapping: dict[str, str],
    new_table_name: str,
    new_schema_description: str,
) -> str:
    mapping_lines = "\n".join(f"- {old} -> {new}" for old, new in sorted(column_mapping.items()))
    return (
        "## Old -> new column mapping (DataHub-verified — use ONLY these names)\n"
        f"{mapping_lines}\n\n"
        "## New table\n"
        f"{new_table_name}\n\n"
        "## New table schema/description (from DataHub)\n"
        f"{new_schema_description}\n\n"
        "## Original file\n"
        f"```sql\n{original_content}\n```\n\n"
        "Rewrite the original file to use the new table and the mapped column "
        "names above. Do not reference any column that is not listed in the "
        "mapping or that was not already present, unchanged, in the original "
        "file. Output only the rewritten SQL."
    )


def generate_rewrite(
    settings: Settings,
    original_content: str,
    column_mapping: dict[str, str],
    new_table_name: str,
    new_schema_description: str,
) -> str:
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key or None)
    prompt = build_codegen_prompt(
        original_content, column_mapping, new_table_name, new_schema_description
    )
    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=8192,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text").strip()
