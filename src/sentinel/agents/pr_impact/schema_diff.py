"""Column-level breaking-change classifier for PR Impact Analysis.

Pure and DataHub-independent by design: given a "before" schema (fetched
from DataHub — the current state of the asset) and an "after" schema
(parsed from the changed file — see the sibling module that extracts a
SELECT list from a SQL/dbt file), classify every column-level change per
the spec's four buckets:

- `column_removed` — always breaking.
- `type_changed` — breaking unless it's a documented safe widening (see
  `_SAFE_WIDENINGS`); unclassifiable (one side's type unknown) is skipped
  rather than guessed.
- `column_added` — always safe.
- `renamed` — breaking, but only detected when an explicit rename hint maps
  old name to new name; without a hint, a rename is indistinguishable from
  an unrelated remove+add pair and is reported as exactly that (this is the
  spec's stated behavior, not an oversight).
"""

from __future__ import annotations

from sentinel.core.models import ColumnChange, ColumnChangeType, SchemaField

# Old-type -> new-type pairs considered a safe widening even though the type
# string changed. Deliberately small and explicit rather than a general
# numeric-type-hierarchy solver — extend it as real cases come up, don't
# guess at exhaustiveness.
_SAFE_WIDENINGS: set[tuple[str, str]] = {
    ("int", "bigint"),
    ("integer", "bigint"),
    ("int32", "int64"),
    ("smallint", "int"),
    ("smallint", "integer"),
    ("smallint", "bigint"),
    ("float", "double"),
    ("real", "double"),
    ("varchar", "text"),
    ("char", "varchar"),
}


def _is_safe_widening(old_type: str, new_type: str) -> bool:
    return (old_type.strip().lower(), new_type.strip().lower()) in _SAFE_WIDENINGS


def _types_differ(old_type: str, new_type: str) -> bool:
    return old_type.strip().lower() != new_type.strip().lower()


def diff_schemas(
    before: list[SchemaField],
    after: list[SchemaField],
    rename_hints: dict[str, str] | None = None,
) -> list[ColumnChange]:
    """`rename_hints` maps old column name -> new column name. Only hints
    where both names actually appear on their respective sides are honored;
    a hint referencing a column that isn't actually there is ignored rather
    than raising, since it's evidence the hint itself is stale, not a reason
    to fail the whole diff."""
    rename_hints = rename_hints or {}
    before_by_name = {f.name: f for f in before}
    after_by_name = {f.name: f for f in after}

    changes: list[ColumnChange] = []
    consumed_before: set[str] = set()
    consumed_after: set[str] = set()

    for old_name, new_name in rename_hints.items():
        if old_name in before_by_name and new_name in after_by_name:
            changes.append(
                ColumnChange(
                    column=old_name,
                    change_type=ColumnChangeType.RENAMED,
                    breaking=True,
                    detail=f"column {old_name!r} renamed to {new_name!r}",
                    renamed_to=new_name,
                )
            )
            consumed_before.add(old_name)
            consumed_after.add(new_name)

    for name, before_field in before_by_name.items():
        if name in consumed_before:
            continue
        after_field = after_by_name.get(name)
        if after_field is None:
            changes.append(
                ColumnChange(
                    column=name,
                    change_type=ColumnChangeType.COLUMN_REMOVED,
                    breaking=True,
                    detail=f"column {name!r} removed",
                )
            )
            continue

        if (
            before_field.type
            and after_field.type
            and _types_differ(before_field.type, after_field.type)
        ):
            breaking = not _is_safe_widening(before_field.type, after_field.type)
            detail = (
                f"column {name!r} type changed from {before_field.type!r} to {after_field.type!r}"
            )
            if not breaking:
                detail += " (safe widening)"
            changes.append(
                ColumnChange(
                    column=name,
                    change_type=ColumnChangeType.TYPE_CHANGED,
                    breaking=breaking,
                    detail=detail,
                    old_type=before_field.type,
                    new_type=after_field.type,
                )
            )

    for name in after_by_name:
        if name in consumed_after or name in before_by_name:
            continue
        changes.append(
            ColumnChange(
                column=name,
                change_type=ColumnChangeType.COLUMN_ADDED,
                breaking=False,
                detail=f"column {name!r} added",
            )
        )

    return changes
