"""Apply a reviewed GeMS Glossary inventory to map and cross-section data.

The script reads the CSV created by GeMS_GlossaryInventory.py. It normalizes
approved spelling and capitalization variants, decomposes legacy compound Type
values where concrete destination values are provided, repairs primary keys in
standard cross-section classes and Glossary, and inserts definitions for all
remaining controlled terms in the selected scope.

The default scope includes GeologicMap, all CrossSection feature datasets, and
DescriptionOfMapUnits. CorrelationOfMapUnits is intentionally excluded. This is
a dry run unless --apply is supplied.
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

from GeMS_GlossaryInventory import DEFINED_TERM_FIELDS, GEMSY_SUFFIXES


REQUIRED_COLUMNS = {
    "Dataset_Path",
    "Field",
    "Current_Value",
    "Suggested_Action",
    "Suggested_Canonical_Term",
    "Suggested_IsConcealed",
    "Suggested_LocationConfidenceMeters",
    "Suggested_ExistenceConfidence",
    "Suggested_IdentityConfidence",
    "Include_In_Cleanup",
    "Review_Decision",
    "Canonical_Term",
    "Definition",
    "DefinitionSourceID",
    "Review_Notes",
}
SKIP_WORDS = {"NO", "N", "FALSE", "EXCLUDE", "SKIP"}
APPLY_ACTIONS = {"NORMALIZE", "NORMALIZE_VALUE", "DECOMPOSE", "DECOMPOSE_TYPE"}
KEEP_ACTIONS = {"", "KEEP", "ADD_GLOSSARY", "REVIEW"}
STANDARD_CROSS_SECTION_ENDINGS = (
    "ContactsAndFaults",
    "GeologicLines",
    "MapUnitPolys",
    "OrientationPoints",
)
AUDIT_FIELDS = (
    "Mode",
    "Record_Type",
    "Dataset_Path",
    "Field",
    "Current_Value",
    "Action",
    "Canonical_Value",
    "Matching_Rows",
    "IsConcealed",
    "LocationConfidenceMeters",
    "ExistenceConfidence",
    "IdentityConfidence",
    "Definition",
    "DefinitionSourceID",
    "Notes",
)


COMMON_DEFINITIONS = {
    "contact": "A surface or line separating two geologic map units.",
    "contact, gradational": "A boundary across which adjacent geologic materials change gradually.",
    "contact, internal": "A boundary within a geologic map unit that does not separate different map units.",
    "fault": "A fracture or zone of fractures along which displacement has occurred.",
    "normal fault": "A fault on which the hanging wall moved downward relative to the footwall.",
    "reverse fault": "A fault on which the hanging wall moved upward relative to the footwall.",
    "thrust fault": "A low-angle reverse fault on which the hanging wall moved upward relative to the footwall.",
    "right lateral strike-slip fault": "A strike-slip fault with right-lateral displacement.",
    "left lateral strike-slip fault": "A strike-slip fault with left-lateral displacement.",
    "oblique right lateral strike-slip fault": "A fault with right-lateral strike-slip and dip-slip displacement components.",
    "map area boundary": "Boundary of the area represented by the geologic map.",
    "water boundary": "Boundary between open water and an adjacent map unit.",
    "lineament": "A linear feature interpreted from topography, imagery, or other geologic evidence.",
    "hydrothermal alteration": "Boundary or trace of an area affected by hydrothermal alteration.",
    "tuff marker bed": "Trace of a tuff bed used as a stratigraphic marker.",
    "anticline": "Trace or representation of an anticline axial surface.",
    "syncline": "Trace or representation of a syncline axial surface.",
    "antiform": "Trace or representation of an antiform axial surface.",
    "synform": "Trace or representation of a synform axial surface.",
    "bedding": "Orientation measurement or representation of bedding.",
    "foliation": "Orientation measurement or representation of foliation.",
    "joint": "Orientation measurement or representation of a joint.",
    "lineation": "Orientation measurement or representation of a linear geologic fabric.",
    "slickenline": "Orientation measurement or representation of a lineation on a fault surface.",
    "volcanic flow": "Orientation measurement or representation of primary volcanic flow structure.",
    "certain": "The feature interpretation is supported by relevant observations and scientific judgment.",
    "questionable": "The existence or identity of the feature cannot be determined with reasonable confidence.",
    "uncertain": "Confidence in the existence or correct identification of the feature is uncertain.",
    "unspecified": "Confidence was not specified or is not available.",
}


def message(arcpy, text):
    try:
        arcpy.AddMessage(text)
    except Exception:
        print(text)


def warning(arcpy, text):
    try:
        arcpy.AddWarning(text)
    except Exception:
        print(f"WARNING: {text}")


def error(arcpy, text):
    try:
        arcpy.AddError(text)
    except Exception:
        print(f"ERROR: {text}", file=sys.stderr)


def text(value):
    return "" if value is None else str(value).strip()


def normalized_action(row):
    decision = text(row.get("Review_Decision")).upper()
    suggested = text(row.get("Suggested_Action")).upper()
    if decision in SKIP_WORDS:
        return "SKIP"
    if decision in ("NORMALIZE", "NORMALIZE_VALUE"):
        return "NORMALIZE_VALUE"
    if decision in ("DECOMPOSE", "DECOMPOSE_TYPE"):
        return "DECOMPOSE_TYPE"
    if decision in ("KEEP", "ADD_GLOSSARY", "REVIEW"):
        return decision
    if suggested in APPLY_ACTIONS | KEEP_ACTIONS:
        return suggested
    raise ValueError(
        f"Unsupported action {decision or suggested!r} for "
        f"{row.get('Dataset_Path')} {row.get('Field')}={row.get('Current_Value')!r}"
    )


def is_included(row):
    return text(row.get("Include_In_Cleanup")).upper() not in SKIP_WORDS


def concrete(value):
    value = text(value)
    return value if value and not value.upper().startswith("REVIEW") else ""


def read_inventory(csv_path):
    with Path(csv_path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = REQUIRED_COLUMNS.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(
                "Inventory is missing required columns: " + ", ".join(sorted(missing))
            )
        rows = list(reader)

    plans = {}
    skipped = set()
    definitions = {}
    for line_number, row in enumerate(rows, start=2):
        dataset_path = text(row["Dataset_Path"]).replace("\\", "/")
        field = text(row["Field"])
        current = text(row["Current_Value"])
        if not dataset_path or not field or not current:
            raise ValueError(f"Line {line_number}: dataset, field, and value are required")
        key = (dataset_path.casefold(), field.casefold(), current)
        if key in plans or key in skipped:
            raise ValueError(
                f"Line {line_number}: duplicate inventory instruction for "
                f"{dataset_path}, {field}, {current!r}"
            )
        if not is_included(row) or normalized_action(row) == "SKIP":
            skipped.add(key)
            continue

        action = normalized_action(row)
        canonical = text(row.get("Canonical_Term")) or text(
            row.get("Suggested_Canonical_Term")
        )
        if action in APPLY_ACTIONS and not canonical:
            raise ValueError(
                f"Line {line_number}: {action} requires Canonical_Term or "
                "Suggested_Canonical_Term"
            )
        if action not in APPLY_ACTIONS:
            canonical = current
        plan = {
            "dataset_path": dataset_path,
            "field": field,
            "current": current,
            "action": action,
            "canonical": canonical,
            "IsConcealed": concrete(row.get("Suggested_IsConcealed")),
            "LocationConfidenceMeters": concrete(
                row.get("Suggested_LocationConfidenceMeters")
            ),
            "ExistenceConfidence": concrete(
                row.get("Suggested_ExistenceConfidence")
            ),
            "IdentityConfidence": concrete(row.get("Suggested_IdentityConfidence")),
            "definition": text(row.get("Definition")),
            "definition_source": text(row.get("DefinitionSourceID")),
            "notes": text(row.get("Review_Notes")),
        }
        plans[key] = plan
        final_term = canonical
        if plan["definition"] or plan["definition_source"]:
            definitions.setdefault(final_term, []).append(plan)
    return plans, skipped, definitions


def relative_path(gdb, catalog_path):
    return os.path.relpath(catalog_path, gdb).replace("\\", "/")


def in_scope(rel_path, scope):
    parts = rel_path.split("/")
    container = parts[0] if len(parts) > 1 else ""
    dataset = parts[-1]
    if dataset in ("Glossary", "GeoMaterialDict", "DataSources"):
        return False
    if scope == "cross-sections":
        return container.startswith("CrossSection")
    if container == "CorrelationOfMapUnits":
        return False
    return (
        container == "GeologicMap"
        or container.startswith("CrossSection")
        or (not container and dataset == "DescriptionOfMapUnits")
    )


def iter_datasets(arcpy, gdb, scope):
    for dirpath, _, filenames in arcpy.da.Walk(
        gdb, datatype=["FeatureClass", "Table"]
    ):
        for filename in filenames:
            catalog_path = os.path.join(dirpath, filename)
            rel = relative_path(gdb, catalog_path)
            if not in_scope(rel, scope):
                continue
            desc = arcpy.Describe(catalog_path)
            if getattr(desc, "featureType", "") == "Annotation":
                continue
            yield rel, catalog_path


def field_map(arcpy, dataset):
    return {field.name.casefold(): field for field in arcpy.ListFields(dataset)}


def matching_plan(plans, rel, field, value):
    return plans.get((rel.casefold(), field.casefold(), value))


def planned_counts(arcpy, datasets, plans):
    counts = {}
    for rel, path in datasets:
        fields = field_map(arcpy, path)
        wanted = sorted(
            {
                plan["field"]
                for key, plan in plans.items()
                if key[0] == rel.casefold() and plan["field"].casefold() in fields
            }
        )
        for field in wanted:
            values = Counter()
            with arcpy.da.SearchCursor(path, [field]) as cursor:
                for (value,) in cursor:
                    values[text(value)] += 1
            for value, count in values.items():
                plan = matching_plan(plans, rel, field, value)
                if plan:
                    counts[(rel.casefold(), field.casefold(), value)] = count
    return counts


def controlled_fields(fields, include_gemsy):
    selected = []
    for field in fields.values():
        if field.type != "String":
            continue
        if field.name in DEFINED_TERM_FIELDS:
            selected.append(field.name)
        elif include_gemsy and field.name.casefold().endswith(GEMSY_SUFFIXES):
            selected.append(field.name)
    return selected


def final_term_contexts(arcpy, datasets, plans, skipped, include_gemsy):
    contexts = defaultdict(lambda: {"fields": set(), "datasets": set(), "count": 0})
    for rel, path in datasets:
        fields = field_map(arcpy, path)
        selected = controlled_fields(fields, include_gemsy)
        if not selected:
            continue
        cursor_fields = list(selected)
        if "GeoMaterialConfidence" in selected and "geomaterial" in fields:
            cursor_fields.append(fields["geomaterial"].name)
        with arcpy.da.SearchCursor(path, cursor_fields) as cursor:
            for record in cursor:
                values = dict(zip(cursor_fields, record))
                excluded = set()
                for field in selected:
                    current = text(values[field])
                    if not current:
                        continue
                    key = (rel.casefold(), field.casefold(), current)
                    if key in skipped:
                        excluded.add(field)
                        continue
                    plan = plans.get(key)
                    if not plan or plan["action"] not in APPLY_ACTIONS:
                        continue
                    values[field] = plan["canonical"]
                    for companion in (
                        "IsConcealed",
                        "LocationConfidenceMeters",
                        "ExistenceConfidence",
                        "IdentityConfidence",
                    ):
                        if plan[companion] and companion in values:
                            values[companion] = plan[companion]

                for field in selected:
                    if field in excluded:
                        continue
                    if field == "GeoMaterialConfidence" and not text(
                        values.get("GeoMaterial")
                    ):
                        continue
                    final = text(values[field])
                    if not final:
                        continue
                    item = contexts[final]
                    item["fields"].add(field)
                    item["datasets"].add(rel)
                    item["count"] += 1
    return contexts


def existing_glossary(arcpy, table):
    rows = {}
    with arcpy.da.SearchCursor(
        table, ["Term", "Definition", "DefinitionSourceID", "Glossary_ID"]
    ) as cursor:
        for term, definition, source, glossary_id in cursor:
            term = text(term)
            if term:
                rows[term] = {
                    "Definition": text(definition),
                    "DefinitionSourceID": text(source),
                    "Glossary_ID": text(glossary_id),
                }
    return rows


def choose_definition(term, context, csv_definitions):
    candidates = csv_definitions.get(term, [])
    explicit_defs = {item["definition"] for item in candidates if item["definition"]}
    if len(explicit_defs) > 1:
        raise ValueError(f"Conflicting reviewed definitions for {term!r}")
    if explicit_defs:
        return explicit_defs.pop()
    if term.casefold() in COMMON_DEFINITIONS:
        return COMMON_DEFINITIONS[term.casefold()]
    fields = ", ".join(sorted(context["fields"]))
    if context["fields"] == {"ParagraphStyle"}:
        return f"Paragraph style used to format {term} entries in DescriptionOfMapUnits."
    if any(field.endswith("Confidence") for field in context["fields"]):
        return f"Project-defined confidence value '{term}' used in {fields}."
    if "Type" in context["fields"]:
        return f"Project-defined geologic or cartographic feature type: {term}."
    return f"Project-defined term '{term}' used in {fields}."


def choose_source(term, csv_definitions, default_source):
    candidates = csv_definitions.get(term, [])
    sources = {item["definition_source"] for item in candidates if item["definition_source"]}
    if len(sources) > 1:
        raise ValueError(f"Conflicting DefinitionSourceID values for {term!r}")
    return sources.pop() if sources else default_source


def validate_source(arcpy, gdb, source_id):
    table = os.path.join(gdb, "DataSources")
    if not arcpy.Exists(table):
        raise ValueError("DataSources table does not exist")
    with arcpy.da.SearchCursor(table, ["DataSources_ID"]) as cursor:
        values = {text(row[0]) for row in cursor}
    if source_id not in values:
        raise ValueError(
            f"Default DefinitionSourceID {source_id!r} is absent from DataSources"
        )


def check_lengths(arcpy, datasets, plans, glossary_table, glossary_inserts):
    paths = {rel.casefold(): path for rel, path in datasets}
    for plan in plans.values():
        if plan["action"] not in APPLY_ACTIONS:
            continue
        path = paths.get(plan["dataset_path"].casefold())
        if not path:
            continue
        fields = field_map(arcpy, path)
        for name, value in [(plan["field"], plan["canonical"])] + [
            (field, plan[field])
            for field in (
                "IsConcealed",
                "LocationConfidenceMeters",
                "ExistenceConfidence",
                "IdentityConfidence",
            )
            if plan[field]
        ]:
            info = fields.get(name.casefold())
            if not info or info.type != "String" or not info.length:
                continue
            if len(value) > info.length:
                raise ValueError(
                    f"{plan['dataset_path']} {name}: {value!r} exceeds field length {info.length}"
                )
    gloss_fields = field_map(arcpy, glossary_table)
    for term, values in glossary_inserts.items():
        for name, value in (
            ("Term", term),
            ("Definition", values["Definition"]),
            ("DefinitionSourceID", values["DefinitionSourceID"]),
        ):
            info = gloss_fields[name.casefold()]
            if info.length and len(value) > info.length:
                raise ValueError(f"Glossary {name} for {term!r} exceeds field length {info.length}")


def standard_cross_section_classes(arcpy, gdb):
    results = []
    for dirpath, _, filenames in arcpy.da.Walk(gdb, datatype=["FeatureClass"]):
        container = os.path.basename(dirpath)
        if not container.startswith("CrossSection"):
            continue
        for filename in filenames:
            if not filename.startswith("CS") or not filename.endswith(STANDARD_CROSS_SECTION_ENDINGS):
                continue
            results.append((relative_path(gdb, os.path.join(dirpath, filename)), os.path.join(dirpath, filename)))
    return results


def id_repairs(arcpy, table, id_field):
    used = set()
    repairs = 0
    with arcpy.da.SearchCursor(table, [id_field]) as cursor:
        for (value,) in cursor:
            value = text(value)
            if not value or value in used:
                repairs += 1
            else:
                used.add(value)
    return repairs


def repair_ids(arcpy, table, id_field, prefix):
    used = set()
    next_number = 1
    changed = 0
    with arcpy.da.UpdateCursor(table, [id_field]) as cursor:
        for row in cursor:
            value = text(row[0])
            if value and value not in used:
                used.add(value)
                continue
            while f"{prefix}{next_number}" in used:
                next_number += 1
            row[0] = f"{prefix}{next_number}"
            used.add(row[0])
            next_number += 1
            cursor.updateRow(row)
            changed += 1
    return changed


def apply_value_updates(arcpy, datasets, plans):
    changed = 0
    for rel, path in datasets:
        fields = field_map(arcpy, path)
        relevant = [
            plan
            for key, plan in plans.items()
            if key[0] == rel.casefold()
            and plan["action"] in APPLY_ACTIONS
            and plan["field"].casefold() in fields
        ]
        by_field = defaultdict(dict)
        for plan in relevant:
            by_field[plan["field"]][plan["current"]] = plan
        for field, value_plans in by_field.items():
            companion_names = [
                name
                for name in (
                    "IsConcealed",
                    "LocationConfidenceMeters",
                    "ExistenceConfidence",
                    "IdentityConfidence",
                )
                if name.casefold() in fields
            ]
            cursor_fields = [field] + companion_names
            with arcpy.da.UpdateCursor(path, cursor_fields) as cursor:
                for row in cursor:
                    plan = value_plans.get(text(row[0]))
                    if not plan:
                        continue
                    row[0] = plan["canonical"]
                    for index, name in enumerate(companion_names, start=1):
                        if plan[name]:
                            info = fields[name.casefold()]
                            row[index] = (
                                float(plan[name])
                                if info.type in ("Double", "Single", "Integer", "SmallInteger")
                                else plan[name]
                            )
                    cursor.updateRow(row)
                    changed += 1
    return changed


def write_audit(path, mode, plans, plan_counts, glossary_inserts, id_summary):
    rows = []
    for key, plan in sorted(plans.items()):
        rows.append(
            {
                "Mode": mode,
                "Record_Type": "VALUE_UPDATE",
                "Dataset_Path": plan["dataset_path"],
                "Field": plan["field"],
                "Current_Value": plan["current"],
                "Action": plan["action"],
                "Canonical_Value": plan["canonical"],
                "Matching_Rows": plan_counts.get(key, 0),
                "IsConcealed": plan["IsConcealed"],
                "LocationConfidenceMeters": plan["LocationConfidenceMeters"],
                "ExistenceConfidence": plan["ExistenceConfidence"],
                "IdentityConfidence": plan["IdentityConfidence"],
                "Definition": plan["definition"],
                "DefinitionSourceID": plan["definition_source"],
                "Notes": plan["notes"],
            }
        )
    for term, values in sorted(glossary_inserts.items(), key=lambda item: item[0].casefold()):
        rows.append(
            {
                "Mode": mode,
                "Record_Type": "GLOSSARY_INSERT",
                "Current_Value": term,
                "Action": "INSERT",
                "Canonical_Value": term,
                "Definition": values["Definition"],
                "DefinitionSourceID": values["DefinitionSourceID"],
                "Matching_Rows": values["Count"],
                "Notes": values["Contexts"],
            }
        )
    for dataset, count in id_summary:
        rows.append(
            {
                "Mode": mode,
                "Record_Type": "ID_REPAIR",
                "Dataset_Path": dataset,
                "Action": "POPULATE_OR_DEDUPLICATE_ID",
                "Matching_Rows": count,
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=AUDIT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gdb", help="Path to the copied file geodatabase")
    parser.add_argument("inventory_csv", help="Reviewed Glossary inventory CSV")
    parser.add_argument(
        "--scope",
        choices=("map-and-cross-sections", "cross-sections"),
        default="map-and-cross-sections",
        help="Datasets to update; CorrelationOfMapUnits is excluded by both options.",
    )
    parser.add_argument(
        "--default-definition-source",
        default="DAS1",
        help="DataSources_ID used for generated definitions (default: DAS1).",
    )
    parser.add_argument(
        "--no-gemsy-fields",
        action="store_true",
        help="Ignore nonstandard string fields ending in Type, Method, or Confidence.",
    )
    parser.add_argument("--audit", help="Optional audit CSV output path")
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
    inventory_csv = Path(args.inventory_csv)
    glossary_table = os.path.join(gdb, "Glossary")
    if not arcpy.Exists(gdb):
        raise ValueError(f"Geodatabase does not exist: {gdb}")
    if not inventory_csv.exists():
        raise ValueError(f"Inventory CSV does not exist: {inventory_csv}")
    if not arcpy.Exists(glossary_table):
        raise ValueError(f"Glossary table does not exist: {glossary_table}")
    glossary_fields = field_map(arcpy, glossary_table)
    required_glossary = {"term", "definition", "definitionsourceid", "glossary_id"}
    if not required_glossary.issubset(glossary_fields):
        missing = sorted(required_glossary.difference(glossary_fields))
        raise ValueError(f"Glossary is missing fields: {', '.join(missing)}")

    plans, skipped, csv_definitions = read_inventory(inventory_csv)
    datasets = list(iter_datasets(arcpy, gdb, args.scope))
    dataset_keys = {rel.casefold() for rel, _ in datasets}
    absent = sorted(
        {plan["dataset_path"] for plan in plans.values() if plan["dataset_path"].casefold() not in dataset_keys}
    )
    for rel in absent:
        warning(arcpy, f"Inventory dataset is outside the selected scope or absent: {rel}")

    validate_source(arcpy, gdb, args.default_definition_source)
    counts = planned_counts(arcpy, datasets, plans)
    contexts = final_term_contexts(
        arcpy,
        datasets,
        plans,
        skipped,
        include_gemsy=not args.no_gemsy_fields,
    )
    existing = existing_glossary(arcpy, glossary_table)
    inserts = {}
    for term, context in contexts.items():
        if term in existing:
            continue
        inserts[term] = {
            "Definition": choose_definition(term, context, csv_definitions),
            "DefinitionSourceID": choose_source(
                term, csv_definitions, args.default_definition_source
            ),
            "Count": context["count"],
            "Contexts": "; ".join(sorted(context["datasets"])),
        }
    for values in inserts.values():
        validate_source(arcpy, gdb, values["DefinitionSourceID"])
    check_lengths(arcpy, datasets, plans, glossary_table, inserts)

    cross_sections = standard_cross_section_classes(arcpy, gdb)
    id_summary = []
    for rel, path in cross_sections:
        id_field = os.path.basename(path) + "_ID"
        fields = field_map(arcpy, path)
        if id_field.casefold() not in fields:
            count = int(arcpy.management.GetCount(path)[0])
        else:
            count = id_repairs(arcpy, path, id_field)
        if count:
            id_summary.append((rel, count))
    glossary_id_repairs = id_repairs(arcpy, glossary_table, "Glossary_ID")
    if glossary_id_repairs:
        id_summary.append(("Glossary", glossary_id_repairs))

    mode = "APPLY" if args.apply else "DRY RUN"
    if args.audit:
        audit_path = Path(args.audit)
    else:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        audit_path = inventory_csv.with_name(f"Glossary_cleanup_audit_{stamp}.csv")
    write_audit(audit_path, mode, plans, counts, inserts, id_summary)

    message(arcpy, f"Mode: {mode}")
    message(arcpy, f"Selected datasets: {len(datasets)}")
    message(arcpy, f"Planned value-update instructions: {sum(1 for p in plans.values() if p['action'] in APPLY_ACTIONS)}")
    message(arcpy, f"Matching feature or table rows: {sum(counts.values())}")
    message(arcpy, f"Glossary rows to insert: {len(inserts)}")
    message(arcpy, f"Rows needing primary-key repair: {sum(count for _, count in id_summary)}")
    message(arcpy, f"Audit written to: {audit_path}")
    if not args.apply:
        message(arcpy, "Dry run complete. No geodatabase values were changed.")
        return 0

    # Adding missing ID fields is a schema operation and must occur before the edit session.
    for _, path in cross_sections:
        id_field = os.path.basename(path) + "_ID"
        if id_field.casefold() not in field_map(arcpy, path):
            arcpy.management.AddField(path, id_field, "TEXT", field_length=50)

    editor = arcpy.da.Editor(gdb)
    editor.startEditing(False, False)
    editor.startOperation()
    try:
        changed = apply_value_updates(arcpy, datasets, plans)
        repaired = 0
        for _, path in cross_sections:
            name = os.path.basename(path)
            repaired += repair_ids(arcpy, path, name + "_ID", name)
        repaired += repair_ids(arcpy, glossary_table, "Glossary_ID", "GLO")

        with arcpy.da.SearchCursor(glossary_table, ["Glossary_ID"]) as cursor:
            used_ids = {text(row[0]) for row in cursor if text(row[0])}
        with arcpy.da.InsertCursor(
            glossary_table,
            ["Term", "Definition", "DefinitionSourceID", "Glossary_ID"],
        ) as cursor:
            next_id = 1
            for term, values in sorted(inserts.items(), key=lambda item: item[0].casefold()):
                while f"GLO{next_id}" in used_ids:
                    next_id += 1
                glossary_id = f"GLO{next_id}"
                used_ids.add(glossary_id)
                next_id += 1
                cursor.insertRow(
                    [term, values["Definition"], values["DefinitionSourceID"], glossary_id]
                )

        editor.stopOperation()
        editor.stopEditing(True)
    except Exception:
        editor.abortOperation()
        editor.stopEditing(False)
        raise

    message(arcpy, f"Updated {changed} feature or table row(s).")
    message(arcpy, f"Repaired {repaired} primary-key value(s).")
    message(arcpy, f"Inserted {len(inserts)} Glossary row(s).")
    message(arcpy, "Apply complete. Rerun GeMS Validate Database and review the audit CSV.")
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
