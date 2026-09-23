"""Apply a reviewed full-database DataSourceID inventory.

All populated string fields whose names end in SourceID are checked. Reviewed
legacy values are replaced with canonical DataSources_ID values in one edit
transaction. The script is a dry run unless --apply is supplied.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
import os
from pathlib import Path
import sys
import time

from GeMS_DataSourceInventory import DAS_RE, iter_source_fields, text


REQUIRED_COLUMNS = {
    "Dataset_Path",
    "Field",
    "Current_SourceID",
    "Feature_Count",
    "Status",
    "Suggested_Canonical_SourceID",
    "Action",
    "Canonical_SourceID",
}
APPLY_WORDS = {"APPLY", "REPLACE", "UPDATE"}
KEEP_WORDS = {"KEEP", "CANONICAL"}
AUDIT_FIELDS = (
    "Mode",
    "Dataset_Path",
    "Field",
    "Current_SourceID",
    "Canonical_SourceID",
    "CSV_Feature_Count",
    "Current_Feature_Count",
    "Action",
    "Status",
    "Review_Notes",
)


def message(arcpy, value):
    try:
        arcpy.AddMessage(value)
    except Exception:
        print(value)


def warning(arcpy, value):
    try:
        arcpy.AddWarning(value)
    except Exception:
        print(f"WARNING: {value}")


def error(arcpy, value):
    try:
        arcpy.AddError(value)
    except Exception:
        print(f"ERROR: {value}", file=sys.stderr)


def canonical_ids(arcpy, gdb):
    table = os.path.join(gdb, "DataSources")
    if not arcpy.Exists(table):
        raise ValueError("DataSources table does not exist")
    with arcpy.da.SearchCursor(table, ["DataSources_ID"]) as cursor:
        return {text(row[0]) for row in cursor if text(row[0])}


def read_inventory(path, valid_ids):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = REQUIRED_COLUMNS.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(
                "Inventory is missing required columns: " + ", ".join(sorted(missing))
            )
        rows = list(reader)

    instructions = {}
    unresolved = []
    for line_number, row in enumerate(rows, start=2):
        rel = text(row["Dataset_Path"]).replace("\\", "/")
        field = text(row["Field"])
        current = text(row["Current_SourceID"])
        action = text(row["Action"]).upper()
        reviewed = text(row["Canonical_SourceID"])
        suggested = text(row["Suggested_Canonical_SourceID"])
        status = text(row["Status"])
        canonical = reviewed or suggested
        key = (rel.casefold(), field.casefold(), current)
        if key in instructions:
            raise ValueError(f"Line {line_number}: duplicate inventory instruction")
        if current in valid_ids or action in KEEP_WORDS:
            instructions[key] = {"row": row, "canonical": current, "apply": False}
            continue
        approved = action in APPLY_WORDS or bool(reviewed)
        if approved:
            if not DAS_RE.fullmatch(canonical):
                raise ValueError(f"Line {line_number}: invalid canonical ID {canonical!r}")
            if canonical not in valid_ids:
                raise ValueError(
                    f"Line {line_number}: {canonical!r} is absent from DataSources"
                )
            instructions[key] = {"row": row, "canonical": canonical, "apply": True}
        else:
            unresolved.append((rel, field, current, suggested, status))
            instructions[key] = {"row": row, "canonical": canonical, "apply": False}
    return instructions, unresolved


def current_counts(arcpy, gdb):
    counts = {}
    paths = {}
    field_names = {}
    for rel, path, fields in iter_source_fields(arcpy, gdb):
        paths[rel.casefold()] = path
        field_names[rel.casefold()] = {field.casefold(): field for field in fields}
        for field in fields:
            counter = Counter()
            with arcpy.da.SearchCursor(path, [field]) as cursor:
                for (value,) in cursor:
                    value = text(value)
                    if value:
                        counter[value] += 1
            for value, count in counter.items():
                counts[(rel.casefold(), field.casefold(), value)] = count
    return counts, paths, field_names


def validate_current(counts, instructions, valid_ids, allow_unresolved):
    unknown = []
    for key, count in counts.items():
        current = key[2]
        if current not in valid_ids and key not in instructions:
            unknown.append((*key, count))
    if unknown and not allow_unresolved:
        detail = "\n  ".join(
            f"{rel}, {field}, {value!r}: {count}" for rel, field, value, count in unknown
        )
        raise ValueError("Current noncanonical values are absent from inventory:\n  " + detail)


def write_audit(path, mode, instructions, counts):
    rows = []
    for key, instruction in sorted(instructions.items()):
        source = instruction["row"]
        rows.append(
            {
                "Mode": mode,
                "Dataset_Path": source["Dataset_Path"],
                "Field": source["Field"],
                "Current_SourceID": source["Current_SourceID"],
                "Canonical_SourceID": instruction["canonical"],
                "CSV_Feature_Count": source["Feature_Count"],
                "Current_Feature_Count": counts.get(key, 0),
                "Action": source["Action"],
                "Status": source["Status"],
                "Review_Notes": source.get("Review_Notes", ""),
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=AUDIT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def apply_updates(arcpy, instructions, paths, field_names):
    grouped = defaultdict(lambda: defaultdict(dict))
    for key, instruction in instructions.items():
        if instruction["apply"]:
            rel, field, current = key
            grouped[rel][field][current] = instruction["canonical"]
    changed = 0
    for rel, fields in grouped.items():
        path = paths.get(rel)
        if not path:
            continue
        for field_key, mappings in fields.items():
            field = field_names[rel][field_key]
            with arcpy.da.UpdateCursor(path, [field]) as cursor:
                for row in cursor:
                    current = text(row[0])
                    canonical = mappings.get(current)
                    if canonical and canonical != current:
                        row[0] = canonical
                        cursor.updateRow(row)
                        changed += 1
    return changed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gdb", help="Path to the copied file geodatabase")
    parser.add_argument("inventory_csv", help="Reviewed full DataSourceID inventory")
    parser.add_argument("--audit", help="Optional audit CSV path")
    parser.add_argument(
        "--allow-unresolved",
        action="store_true",
        help="Apply reviewed mappings while leaving unresolved identifiers unchanged.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit changes. Without this flag the script only reports proposed changes.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        import arcpy
    except ImportError as exc:
        raise RuntimeError("Run this script with the ArcGIS Pro Python environment") from exc
    gdb = str(Path(args.gdb))
    inventory_path = Path(args.inventory_csv)
    if not arcpy.Exists(gdb):
        raise ValueError(f"Geodatabase does not exist: {gdb}")
    if not inventory_path.exists():
        raise ValueError(f"Inventory CSV does not exist: {inventory_path}")

    valid_ids = canonical_ids(arcpy, gdb)
    instructions, unresolved = read_inventory(inventory_path, valid_ids)
    counts, paths, field_names = current_counts(arcpy, gdb)
    validate_current(counts, instructions, valid_ids, args.allow_unresolved)
    if unresolved and not args.allow_unresolved:
        detail = "\n  ".join(
            f"{rel}, {field}, {current!r}; suggestion={suggested!r} ({status})"
            for rel, field, current, suggested, status in unresolved
        )
        raise ValueError(
            "Inventory still has unresolved rows. Review them or use "
            "--allow-unresolved to leave them unchanged:\n  " + detail
        )

    mode = "APPLY" if args.apply else "DRY RUN"
    if args.audit:
        audit_path = Path(args.audit)
    else:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        audit_path = inventory_path.with_name(f"All_DataSourceID_audit_{stamp}.csv")
    write_audit(audit_path, mode, instructions, counts)
    update_count = sum(
        counts.get(key, 0)
        for key, instruction in instructions.items()
        if instruction["apply"] and key[2] != instruction["canonical"]
    )
    message(arcpy, f"Mode: {mode}")
    message(arcpy, f"Source fields inventoried: {len({key[:2] for key in counts})}")
    message(arcpy, f"Rows to update: {update_count}")
    message(arcpy, f"Unresolved inventory rows: {len(unresolved)}")
    message(arcpy, f"Audit written to: {audit_path}")
    if not args.apply:
        message(arcpy, "Dry run complete. No geodatabase values were changed.")
        return 0

    editor = arcpy.da.Editor(gdb)
    editor.startEditing(False, False)
    editor.startOperation()
    try:
        changed = apply_updates(arcpy, instructions, paths, field_names)
        editor.stopOperation()
        editor.stopEditing(True)
    except Exception:
        editor.abortOperation()
        editor.stopEditing(False)
        raise
    message(arcpy, f"Updated {changed} source-reference value(s).")
    message(arcpy, "Apply complete. Rerun GeMS Validate Database.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        try:
            import arcpy
        except ImportError:
            arcpy = None
        error(arcpy, str(exc))
        sys.exit(1)
