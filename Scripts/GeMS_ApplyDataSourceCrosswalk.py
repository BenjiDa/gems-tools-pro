"""Apply a reviewed DataSourceID crosswalk to ContactsAndFaults.

This is a stand-alone ArcGIS Pro script. It reads the editable CSV produced for
the Healdsburg source cleanup, validates its contents against the geodatabase,
updates ContactsAndFaults.DataSourceID, and creates or updates DataSources rows.

The script is a dry run unless --apply is supplied. Always use a geodatabase
copy and review the dry-run output before applying changes.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path
import re
import sys
import time


REQUIRED_COLUMNS = {
    "SourceID_As_Entered",
    "Feature_Count",
    "Canonical_SourceID",
    "References notes",
}
CANONICAL_ID_RE = re.compile(r"^DAS[1-9]\d*$")
URL_RE = re.compile(r"https?://[^\s]+")


def message(text):
    try:
        import arcpy

        arcpy.AddMessage(text)
    except Exception:
        print(text)


def warning(text):
    try:
        import arcpy

        arcpy.AddWarning(text)
    except Exception:
        print(f"WARNING: {text}")


def error(text):
    try:
        import arcpy

        arcpy.AddError(text)
    except Exception:
        print(f"ERROR: {text}", file=sys.stderr)


def extract_url(citation):
    match = URL_RE.search(citation or "")
    return match.group(0).rstrip(".,") if match else None


def read_crosswalk(csv_path):
    """Return raw-ID mappings and one planned DataSources row per DAS ID."""
    with Path(csv_path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing_columns = REQUIRED_COLUMNS.difference(reader.fieldnames or [])
        if missing_columns:
            raise ValueError(
                "Crosswalk is missing required columns: "
                + ", ".join(sorted(missing_columns))
            )
        rows = list(reader)

    mappings = {}
    expected_counts = {}
    grouped = defaultdict(list)
    unresolved = []

    for line_number, row in enumerate(rows, start=2):
        raw_id = (row["SourceID_As_Entered"] or "").strip()
        canonical_id = (row["Canonical_SourceID"] or "").strip()
        citation = (row["References notes"] or "").strip()
        count_text = (row["Feature_Count"] or "").strip()

        if not raw_id:
            raise ValueError(f"Line {line_number}: SourceID_As_Entered is blank")
        try:
            expected_count = int(count_text)
        except ValueError as exc:
            raise ValueError(
                f"Line {line_number}: Feature_Count is not an integer: {count_text!r}"
            ) from exc
        if expected_count < 0:
            raise ValueError(f"Line {line_number}: Feature_Count cannot be negative")
        if raw_id in expected_counts:
            raise ValueError(f"Duplicate crosswalk row for {raw_id!r}")
        expected_counts[raw_id] = expected_count

        if not canonical_id:
            unresolved.append(raw_id)
            continue
        if not CANONICAL_ID_RE.fullmatch(canonical_id):
            raise ValueError(
                f"Line {line_number}: invalid canonical ID {canonical_id!r}; "
                "expected DAS followed by a positive integer"
            )
        if not citation:
            raise ValueError(
                f"Line {line_number}: {canonical_id} has no citation in References notes"
            )

        mappings[raw_id] = canonical_id
        grouped[canonical_id].append((raw_id, citation))

    source_rows = {}
    for canonical_id, items in grouped.items():
        citations = {citation for _, citation in items}
        if len(citations) != 1:
            detail = "; ".join(sorted(citations))
            raise ValueError(
                f"{canonical_id} has conflicting citations in the crosswalk: {detail}"
            )
        citation = citations.pop()
        source_text = (
            "This report"
            if canonical_id == "DAS1" and citation.casefold() == "this study"
            else citation
        )
        legacy_ids = sorted(raw_id for raw_id, _ in items)
        source_rows[canonical_id] = {
            "Source": source_text,
            "Notes": "Legacy SourceID values consolidated: " + "; ".join(legacy_ids),
            "URL": extract_url(citation),
        }

    return mappings, expected_counts, source_rows, unresolved


def field_info(arcpy, table):
    return {field.name: field for field in arcpy.ListFields(table)}


def validate_fields(arcpy, table, required):
    fields = field_info(arcpy, table)
    missing = set(required).difference(fields)
    if missing:
        raise ValueError(f"{table} is missing fields: {', '.join(sorted(missing))}")
    return fields


def check_lengths(fields, source_rows):
    for canonical_id, values in source_rows.items():
        for field_name in ("DataSources_ID", "Source", "Notes", "URL"):
            value = canonical_id if field_name == "DataSources_ID" else values[field_name]
            if value is None:
                continue
            limit = fields[field_name].length
            if limit and len(value) > limit:
                raise ValueError(
                    f"{canonical_id} {field_name} has {len(value)} characters; "
                    f"the field limit is {limit}"
                )


def get_feature_counts(arcpy, feature_class):
    counts = Counter()
    with arcpy.da.SearchCursor(feature_class, ["DataSourceID"]) as cursor:
        for (value,) in cursor:
            counts["<NULL>" if value is None or not str(value).strip() else str(value).strip()] += 1
    return counts


def validate_counts(actual, expected, canonical_ids, allow_mismatch):
    """Confirm that the CSV and geodatabase describe the same feature set.

    Individual source counts may differ after a user has already reassigned some
    features or after this script has been run once. Canonical DAS identifiers
    are therefore valid alongside the legacy identifiers listed in the CSV.
    The total feature count must still match, and every current identifier must
    be represented by either a CSV row or a planned canonical DataSources row.
    """
    problems = []
    expected_total = sum(expected.values())
    actual_total = sum(actual.values())
    if actual_total != expected_total:
        problems.append(
            f"CSV total {expected_total}, geodatabase total {actual_total}"
        )

    known_ids = set(expected).union(canonical_ids)
    unexpected = sorted(set(actual).difference(known_ids), key=str.casefold)
    for source_id in unexpected:
        problems.append(
            f"{source_id!r} occurs {actual[source_id]} time(s) but is neither a "
            "legacy CSV identifier nor a planned canonical DAS identifier"
        )

    if problems:
        text = "Feature inventory does not match:\n  " + "\n  ".join(problems)
        if allow_mismatch:
            warning(text)
        else:
            raise ValueError(
                text
                + "\nUse --allow-count-mismatch only after reviewing the differences."
            )

    changed_counts = []
    for raw_id in sorted(expected, key=str.casefold):
        actual_count = actual.get(raw_id, 0)
        if actual_count != expected[raw_id]:
            changed_counts.append(
                f"{raw_id!r}: CSV snapshot {expected[raw_id]}, current {actual_count}"
            )
    if changed_counts:
        warning(
            "Individual source counts changed since the CSV inventory was created. "
            "This is expected after manual reassignment or a prior script run:\n  "
            + "\n  ".join(changed_counts)
        )


def existing_sources(arcpy, table):
    rows = {}
    duplicates = set()
    fields = ["DataSources_ID", "Source", "Notes", "URL"]
    with arcpy.da.SearchCursor(table, fields) as cursor:
        for source_id, source, notes, url in cursor:
            if source_id in rows:
                duplicates.add(source_id)
            rows[source_id] = {"Source": source, "Notes": notes, "URL": url}
    if duplicates:
        raise ValueError(
            "DataSources has duplicate primary keys: "
            + ", ".join(repr(value) for value in sorted(duplicates, key=str))
        )
    return rows


def compare_sources(existing, planned):
    inserts = []
    updates = []
    unchanged = []
    for source_id, values in sorted(
        planned.items(), key=lambda item: int(item[0][3:])
    ):
        if source_id not in existing:
            inserts.append(source_id)
        elif existing[source_id] == values:
            unchanged.append(source_id)
        else:
            updates.append(source_id)
    return inserts, updates, unchanged


def write_audit(path, mappings, expected, actual, source_rows, mode):
    fields = [
        "Mode",
        "Original_SourceID",
        "Canonical_SourceID",
        "CSV_Feature_Count",
        "GDB_Feature_Count",
        "Source",
        "URL",
    ]
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for raw_id in sorted(expected, key=lambda value: (value == "<NULL>", value.casefold())):
            canonical_id = mappings.get(raw_id, "")
            source = source_rows.get(canonical_id, {})
            writer.writerow(
                {
                    "Mode": mode,
                    "Original_SourceID": raw_id,
                    "Canonical_SourceID": canonical_id,
                    "CSV_Feature_Count": expected[raw_id],
                    "GDB_Feature_Count": actual.get(raw_id, 0),
                    "Source": source.get("Source", ""),
                    "URL": source.get("URL", "") or "",
                }
            )


def apply_changes(
    arcpy,
    gdb,
    feature_class,
    sources_table,
    mappings,
    source_rows,
    inserts,
    updates,
    overwrite_existing,
):
    if updates and not overwrite_existing:
        raise ValueError(
            "Existing DataSources rows conflict with the CSV for: "
            + ", ".join(updates)
            + ". Review them, then rerun with --overwrite-existing if replacement is intended."
        )

    editor = arcpy.da.Editor(gdb)
    editor.startEditing(False, False)
    editor.startOperation()
    try:
        changed = 0
        with arcpy.da.UpdateCursor(feature_class, ["DataSourceID"]) as cursor:
            for row in cursor:
                raw_id = "<NULL>" if row[0] is None or not str(row[0]).strip() else str(row[0]).strip()
                canonical_id = mappings.get(raw_id)
                if canonical_id and row[0] != canonical_id:
                    row[0] = canonical_id
                    cursor.updateRow(row)
                    changed += 1

        if updates:
            with arcpy.da.UpdateCursor(
                sources_table, ["DataSources_ID", "Source", "Notes", "URL"]
            ) as cursor:
                for row in cursor:
                    if row[0] in updates:
                        values = source_rows[row[0]]
                        row[1:] = [values["Source"], values["Notes"], values["URL"]]
                        cursor.updateRow(row)

        if inserts:
            with arcpy.da.InsertCursor(
                sources_table, ["DataSources_ID", "Source", "Notes", "URL"]
            ) as cursor:
                for source_id in inserts:
                    values = source_rows[source_id]
                    cursor.insertRow(
                        [source_id, values["Source"], values["Notes"], values["URL"]]
                    )

        editor.stopOperation()
        editor.stopEditing(True)
        return changed
    except Exception:
        editor.abortOperation()
        editor.stopEditing(False)
        raise


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gdb", help="Path to the copied file geodatabase")
    parser.add_argument("crosswalk", help="Path to the reviewed source inventory CSV")
    parser.add_argument(
        "--feature-class",
        default="GeologicMap/ContactsAndFaults",
        help="Path inside the geodatabase (default: GeologicMap/ContactsAndFaults)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit changes. Without this flag the script only reports proposed changes.",
    )
    parser.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="Replace conflicting existing DataSources rows during --apply.",
    )
    parser.add_argument(
        "--allow-count-mismatch",
        action="store_true",
        help=(
            "Continue when the total feature count differs or current source identifiers "
            "are absent from the CSV and canonical DAS list."
        ),
    )
    parser.add_argument("--audit", help="Optional output audit CSV path")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        import arcpy
    except ImportError as exc:
        raise RuntimeError("Run this script with the ArcGIS Pro Python environment") from exc

    gdb = str(Path(args.gdb))
    feature_class = str(Path(gdb) / Path(args.feature_class))
    sources_table = str(Path(gdb) / "DataSources")
    csv_path = Path(args.crosswalk)

    if not arcpy.Exists(gdb):
        raise ValueError(f"Geodatabase does not exist: {gdb}")
    if not arcpy.Exists(feature_class):
        raise ValueError(f"Feature class does not exist: {feature_class}")
    if not arcpy.Exists(sources_table):
        raise ValueError(f"DataSources table does not exist: {sources_table}")

    validate_fields(arcpy, feature_class, ["DataSourceID"])
    source_fields = validate_fields(
        arcpy, sources_table, ["DataSources_ID", "Source", "Notes", "URL"]
    )
    mappings, expected, source_rows, unresolved = read_crosswalk(csv_path)
    check_lengths(source_fields, source_rows)

    actual = get_feature_counts(arcpy, feature_class)
    validate_counts(actual, expected, set(source_rows), args.allow_count_mismatch)
    existing = existing_sources(arcpy, sources_table)
    inserts, updates, unchanged = compare_sources(existing, source_rows)

    mode = "APPLY" if args.apply else "DRY RUN"
    message(f"Mode: {mode}")
    message(f"Feature class: {feature_class}")
    message(f"Mapped legacy identifiers: {len(mappings)}")
    message(f"Canonical DataSources rows: {len(source_rows)}")
    message(f"DataSources rows to insert: {len(inserts)}")
    message(f"DataSources rows that conflict: {len(updates)}")
    message(f"DataSources rows already identical: {len(unchanged)}")
    for raw_id in unresolved:
        remaining = actual.get(raw_id, 0)
        if remaining:
            warning(
                f"{raw_id!r} has no Canonical_SourceID and will remain unchanged "
                f"({remaining} feature(s))"
            )
    for source_id, values in source_rows.items():
        if "find citation" in values["Source"].casefold():
            warning(f"{source_id} still contains an incomplete citation: {values['Source']}")

    if args.audit:
        audit_path = Path(args.audit)
    else:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        audit_path = csv_path.with_name(f"DataSource_cleanup_audit_{stamp}.csv")
    write_audit(audit_path, mappings, expected, actual, source_rows, mode)
    message(f"Audit written to: {audit_path}")

    if not args.apply:
        message("Dry run complete. No geodatabase values were changed.")
        return 0

    changed = apply_changes(
        arcpy,
        gdb,
        feature_class,
        sources_table,
        mappings,
        source_rows,
        inserts,
        updates,
        args.overwrite_existing,
    )
    message(f"Updated {changed} ContactsAndFaults feature(s).")
    message(f"Inserted {len(inserts)} DataSources row(s).")
    message(f"Updated {len(updates)} existing DataSources row(s).")
    message("Apply complete. Rerun GeMS Validate Database and review the audit CSV.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        error(str(exc))
        sys.exit(1)
