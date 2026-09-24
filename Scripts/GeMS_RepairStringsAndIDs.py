"""Repair reported blank text values and primary IDs in a GeMS geodatabase.

By default, the script is limited to the datasets and fields reported by the
current Healdsburg GeMS validation. It converts empty or whitespace-only
strings to NULL, trims leading and trailing whitespace, and repairs only
missing or duplicated values in each selected <Dataset>_ID field. Existing
unique primary IDs are preserved. Use --all-datasets only for a deliberate
database-wide cleanup. The default mode is a dry run.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
import os
from pathlib import Path
import re
import sys
import time


ID_PREFIXES = {
    "CartographicLines": "CAL",
    "ContactsAndFaults": "CAF",
    "CMULines": "CMULIN",
    "CMUMapUnitPolys": "CMUMUP",
    "CMUPoints": "CMUPNT",
    "CMUText": "CMUTXT",
    "DataSources": "DAS",
    "DataSourcePolys": "DSP",
    "DescriptionOfMapUnits": "DMU",
    "ExtendedAttributes": "EXA",
    "FossilPoints": "FSP",
    "GenericPoints": "GNP",
    "GenericSamples": "GNS",
    "GeochemPoints": "GCM",
    "GeochronPoints": "GCR",
    "GeologicEvents": "GEE",
    "GeologicLines": "GEL",
    "Glossary": "GLO",
    "IsoValueLines": "IVL",
    "MapUnitPoints": "MPT",
    "MapUnitPolys": "MUP",
    "MapUnitOverlayPolys": "MUO",
    "MiscellaneousMapInformation": "MMI",
    "OrientationPoints": "ORP",
    "OtherLines": "OTL",
    "OverlayPolys": "OVP",
    "PhotoPoints": "PHP",
    "RepurposedSymbols": "RPS",
    "Stations": "STA",
    "StandardLithology": "STL",
}

# Scope from the 2026-09-23 Healdsburg validation report. Keeping this scope
# explicit prevents empty optional fields in intermediate datasets from being
# altered merely because they are stored as empty strings.
DEFAULT_STRING_FIELDS = {
    "ContactsAndFaults": {"Label", "Notes"},
    "GeologicLines": {"Label"},
    "CSAContactsAndFaults": {"Notes"},
    "CSAGeologicLines": {"Label"},
}
DEFAULT_ID_DATASETS = {
    "CMUPoints",
    "ContactsAndFaults",
    "MapUnitPoints",
    "CartographicLines",
    "DataSourcePolys",
    "CMULines",
    "MapUnitPolys",
    "OrientationPoints",
    "GeologicLines",
}

BAD_ID_TEXT = {"none", "null", "<null>"}
AUDIT_FIELDS = (
    "Mode",
    "Action",
    "Dataset_Path",
    "Dataset",
    "OBJECTID",
    "Field",
    "Old_Value",
    "New_Value",
)


def message(arcpy, value):
    try:
        arcpy.AddMessage(value)
    except Exception:
        print(value)


def error(arcpy, value):
    try:
        arcpy.AddError(value)
    except Exception:
        print(f"ERROR: {value}", file=sys.stderr)


def text(value):
    return "" if value is None else str(value)


def relative_path(gdb, path):
    return os.path.relpath(path, gdb).replace("\\", "/")


def iter_datasets(arcpy, gdb):
    for dirpath, _, names in arcpy.da.Walk(
        gdb, datatype=["FeatureClass", "Table"]
    ):
        for name in names:
            path = os.path.join(dirpath, name)
            desc = arcpy.Describe(path)
            if getattr(desc, "featureType", "") == "Annotation":
                continue
            oid_field = getattr(desc, "OIDFieldName", "")
            if not oid_field:
                message(arcpy, f"Skipping dataset without an ObjectID field: {path}")
                continue
            yield relative_path(gdb, path), path, name, oid_field


def field_map(arcpy, path):
    return {field.name.casefold(): field for field in arcpy.ListFields(path)}


def invalid_id(value):
    if value is None:
        return True
    stripped = str(value).strip()
    return not stripped or stripped.casefold() in BAD_ID_TEXT


def fallback_prefix(dataset):
    words = re.findall(
        r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+", dataset
    )
    letters = "".join(word[0] for word in words if not word.isdigit()).upper()
    return letters or re.sub(r"[^A-Za-z0-9]", "", dataset).upper()[:8] or "ID"


def choose_prefix(dataset, preserved_values):
    if dataset in ID_PREFIXES:
        return ID_PREFIXES[dataset]
    prefixes = Counter()
    for value in preserved_values:
        match = re.fullmatch(r"(.+?)(\d+)", value)
        if match:
            prefixes[match.group(1)] += 1
    if prefixes:
        return prefixes.most_common(1)[0][0]
    return fallback_prefix(dataset)


def next_ids(prefix, reserved):
    suffixes = []
    for value in reserved:
        match = re.fullmatch(re.escape(prefix) + r"(\d+)", value)
        if match:
            suffixes.append(int(match.group(1)))
    number = max(suffixes, default=0) + 1
    while True:
        candidate = f"{prefix}{number}"
        number += 1
        if candidate not in reserved:
            reserved.add(candidate)
            yield candidate


def plan_primary_ids(rows, dataset):
    """Return (oid, old, new, action) repairs, preserving the first duplicate."""
    seen = set()
    preserved = set()
    pending = []
    trims = []
    for oid, raw in sorted(rows):
        if invalid_id(raw):
            pending.append((oid, raw, "GENERATE_MISSING_ID"))
            continue
        cleaned = str(raw).strip()
        if cleaned in seen:
            pending.append((oid, raw, "REPLACE_DUPLICATE_ID"))
            continue
        seen.add(cleaned)
        preserved.add(cleaned)
        if raw != cleaned:
            trims.append((oid, raw, cleaned, "TRIM_PRIMARY_ID"))

    prefix = choose_prefix(dataset, preserved)
    generator = next_ids(prefix, set(preserved))
    repairs = list(trims)
    for oid, raw, action in pending:
        repairs.append((oid, raw, next(generator), action))
    return sorted(repairs, key=lambda item: item[0])


def build_plan(arcpy, gdb, all_datasets=False):
    plans = []
    dataset_paths = {}
    for rel, path, dataset, oid_field in iter_datasets(arcpy, gdb):
        if not all_datasets and dataset not in (
            set(DEFAULT_STRING_FIELDS) | DEFAULT_ID_DATASETS
        ):
            continue
        dataset_paths[rel] = {"path": path, "oid_field": oid_field}
        fields = field_map(arcpy, path)
        id_name = f"{dataset}_ID"
        id_info = fields.get(id_name.casefold())
        primary_key = (
            id_info.name
            if id_info
            and id_info.type == "String"
            and (all_datasets or dataset in DEFAULT_ID_DATASETS)
            else None
        )

        if all_datasets:
            string_fields = [
                field.name
                for field in fields.values()
                if field.type == "String"
                and field.editable
                and field.name != primary_key
            ]
        else:
            selected = {
                name.casefold() for name in DEFAULT_STRING_FIELDS.get(dataset, set())
            }
            string_fields = [
                field.name
                for field in fields.values()
                if field.name.casefold() in selected
                and field.type == "String"
                and field.editable
            ]
        if string_fields:
            with arcpy.da.SearchCursor(path, [oid_field] + string_fields) as cursor:
                for row in cursor:
                    oid = row[0]
                    for index, field in enumerate(string_fields, start=1):
                        old = row[index]
                        if old is None:
                            continue
                        new = str(old).strip()
                        if not new:
                            plans.append(
                                {
                                    "Action": "BLANK_TO_NULL",
                                    "Dataset_Path": rel,
                                    "Dataset": dataset,
                                    "OBJECTID": oid,
                                    "Field": field,
                                    "Old_Value": old,
                                    "New_Value": None,
                                }
                            )
                        elif new != old:
                            plans.append(
                                {
                                    "Action": "TRIM_TEXT",
                                    "Dataset_Path": rel,
                                    "Dataset": dataset,
                                    "OBJECTID": oid,
                                    "Field": field,
                                    "Old_Value": old,
                                    "New_Value": new,
                                }
                            )

        if primary_key:
            with arcpy.da.SearchCursor(path, [oid_field, primary_key]) as cursor:
                id_rows = list(cursor)
            for oid, old, new, action in plan_primary_ids(id_rows, dataset):
                if id_info.length and len(new) > id_info.length:
                    raise ValueError(
                        f"Generated ID {new!r} exceeds {rel}.{primary_key} length "
                        f"{id_info.length}"
                    )
                plans.append(
                    {
                        "Action": action,
                        "Dataset_Path": rel,
                        "Dataset": dataset,
                        "OBJECTID": oid,
                        "Field": primary_key,
                        "Old_Value": old,
                        "New_Value": new,
                    }
                )
    return plans, dataset_paths


def display_value(value):
    if value is None:
        return "<NULL>"
    if value == "":
        return "<EMPTY>"
    if str(value).strip() == "":
        return "<WHITESPACE>"
    return str(value)


def write_audit(path, mode, plans):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=AUDIT_FIELDS)
        writer.writeheader()
        for plan in plans:
            row = dict(plan)
            row["Mode"] = mode
            row["Old_Value"] = display_value(row["Old_Value"])
            row["New_Value"] = display_value(row["New_Value"])
            writer.writerow(row)


def apply_plan(arcpy, gdb, plans, dataset_paths):
    grouped = defaultdict(lambda: defaultdict(dict))
    for plan in plans:
        grouped[plan["Dataset_Path"]][plan["Field"]][plan["OBJECTID"]] = plan[
            "New_Value"
        ]

    changed = 0
    editor = arcpy.da.Editor(gdb)
    editor.startEditing(False, False)
    editor.startOperation()
    try:
        for rel, fields in grouped.items():
            path = dataset_paths[rel]["path"]
            oid_field = dataset_paths[rel]["oid_field"]
            field_names = sorted(fields, key=str.casefold)
            with arcpy.da.UpdateCursor(path, [oid_field] + field_names) as cursor:
                for row in cursor:
                    oid = row[0]
                    row_changed = False
                    for index, field in enumerate(field_names, start=1):
                        if oid in fields[field]:
                            row[index] = fields[field][oid]
                            changed += 1
                            row_changed = True
                    if row_changed:
                        cursor.updateRow(row)
        editor.stopOperation()
        editor.stopEditing(True)
    except Exception:
        editor.abortOperation()
        editor.stopEditing(False)
        raise
    return changed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gdb", help="Path to the copied file geodatabase")
    parser.add_argument("--audit", help="Optional audit CSV path")
    parser.add_argument(
        "--all-datasets",
        action="store_true",
        help=(
            "Scan every non-annotation dataset and editable string field. "
            "Without this flag, use the validation-report cleanup scope."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit changes. Without this flag the script only reports them.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        import arcpy
    except ImportError as exc:
        raise RuntimeError("Run this script with the ArcGIS Pro Python environment") from exc

    gdb = str(Path(args.gdb))
    if not arcpy.Exists(gdb):
        raise ValueError(f"Geodatabase does not exist: {gdb}")

    plans, dataset_paths = build_plan(arcpy, gdb, args.all_datasets)
    mode = "APPLY" if args.apply else "DRY RUN"
    if args.audit:
        audit_path = Path(args.audit)
    else:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        audit_path = Path(gdb).with_name(f"String_ID_cleanup_audit_{stamp}.csv")
    write_audit(audit_path, mode, plans)

    counts = Counter(plan["Action"] for plan in plans)
    message(arcpy, f"Mode: {mode}")
    message(
        arcpy,
        "Scope: " + ("ALL DATASETS" if args.all_datasets else "VALIDATION REPORT"),
    )
    message(arcpy, f"Datasets scanned: {len(dataset_paths)}")
    message(arcpy, f"Blank strings to convert to NULL: {counts['BLANK_TO_NULL']}")
    message(arcpy, f"Nonempty strings to trim: {counts['TRIM_TEXT']}")
    message(arcpy, f"Primary IDs to trim: {counts['TRIM_PRIMARY_ID']}")
    message(arcpy, f"Missing primary IDs to generate: {counts['GENERATE_MISSING_ID']}")
    message(arcpy, f"Duplicated primary IDs to replace: {counts['REPLACE_DUPLICATE_ID']}")
    message(arcpy, f"Audit written to: {audit_path}")

    if not args.apply:
        message(arcpy, "Dry run complete. No geodatabase values were changed.")
        return 0

    changed = apply_plan(arcpy, gdb, plans, dataset_paths)
    message(arcpy, f"Apply complete. Field values changed: {changed}")
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
