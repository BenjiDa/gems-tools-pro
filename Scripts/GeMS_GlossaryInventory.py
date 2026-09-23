"""Create a review CSV for GeMS Glossary cleanup.

This stand-alone ArcGIS Pro script inventories values in fields whose terms
must be defined in Glossary. It does not modify the geodatabase. The output is
one row per dataset, field, and distinct value, with feature counts, cautious
normalization suggestions, and blank review columns for the map author.
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


DEFINED_TERM_FIELDS = {
    "Type",
    "ExistenceConfidence",
    "IdentityConfidence",
    "ParagraphStyle",
    "GeoMaterialConfidence",
    "ErrorMeasure",
    "AgeUnits",
    "LocationMethod",
    "ScientificConfidence",
    "ValueConfidence",
}
GEMSY_SUFFIXES = ("type", "method", "confidence")
LINE_COMPANION_FIELDS = (
    "IsConcealed",
    "LocationConfidenceMeters",
    "ExistenceConfidence",
    "IdentityConfidence",
)
OUTPUT_FIELDS = (
    "Dataset_Path",
    "Dataset",
    "Feature_Dataset",
    "Field",
    "Field_Check",
    "Current_Value",
    "Feature_Count",
    "Glossary_Status",
    "Companion_IsConcealed",
    "Companion_LocationConfidenceMeters",
    "Companion_ExistenceConfidence",
    "Companion_IdentityConfidence",
    "Suggested_Action",
    "Suggested_Canonical_Term",
    "Suggested_IsConcealed",
    "Suggested_LocationConfidenceMeters",
    "Suggested_ExistenceConfidence",
    "Suggested_IdentityConfidence",
    "Suggestion_Reason",
    "Include_In_Cleanup",
    "Review_Decision",
    "Canonical_Term",
    "Definition",
    "DefinitionSourceID",
    "Review_Notes",
)


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


def display_value(value):
    if value is None:
        return ""
    return str(value).strip()


def summarize(counter):
    if not counter:
        return ""
    return "; ".join(
        f"{display_value(value)} [{count}]"
        for value, count in sorted(
            counter.items(), key=lambda item: (-item[1], display_value(item[0]).casefold())
        )
    )


def clean_type(value):
    """Return a cautious decomposition suggestion for a legacy Type value."""
    original = value.strip()
    corrected = re.sub(r"\bquestionale\b", "questionable", original, flags=re.I)
    parts = [part.strip() for part in corrected.split(",") if part.strip()]
    if not parts:
        return None

    qualifiers = {
        "certain",
        "concealed",
        "queried",
        "questionable",
        "inferred",
        "approx. located",
        "approximately located",
        "approx located",
    }
    found = [part.casefold() for part in parts[1:] if part.casefold() in qualifiers]
    base_parts = [parts[0]] + [
        part for part in parts[1:] if part.casefold() not in qualifiers
    ]
    base = ", ".join(base_parts).strip()

    result = {
        "action": "",
        "term": "",
        "concealed": "",
        "location": "",
        "existence": "",
        "identity": "",
        "reason": "",
    }
    if original != corrected:
        result.update(
            action="NORMALIZE_VALUE",
            term=base,
            reason="Correct apparent misspelling; review intended confidence.",
        )

    if found:
        result["action"] = "DECOMPOSE_TYPE"
        result["term"] = base
        reasons = ["Move legacy qualifiers out of Type into GeMS attribute fields."]
        if "concealed" in found:
            result["concealed"] = "Y"
        if any(q in found for q in ("approx. located", "approximately located", "approx located", "inferred")):
            result["location"] = "REVIEW numeric meters"
        if "certain" in found:
            result["existence"] = "certain"
            result["identity"] = "certain"
        if any(q in found for q in ("queried", "questionable")):
            result["existence"] = "REVIEW"
            result["identity"] = "REVIEW"
            reasons.append(
                "Queried features require author review to distinguish existence from identity confidence."
            )
        result["reason"] = " ".join(reasons)
    return result


def make_suggestion(field_name, value, case_variants):
    result = {
        "action": "ADD_GLOSSARY",
        "term": value,
        "concealed": "",
        "location": "",
        "existence": "",
        "identity": "",
        "reason": "Term is used but is not currently defined in Glossary.",
    }

    if len(case_variants.get(value.casefold(), set())) > 1:
        preferred = sorted(
            case_variants[value.casefold()],
            key=lambda item: (item != item.casefold(), item.casefold(), item),
        )[0]
        if value != preferred:
            result.update(
                action="NORMALIZE_VALUE",
                term=preferred,
                reason="Case variant of another value in the geodatabase.",
            )

    if field_name == "Type":
        type_result = clean_type(value)
        if type_result and type_result["action"]:
            result.update(type_result)
    elif field_name in (
        "ExistenceConfidence",
        "IdentityConfidence",
        "ScientificConfidence",
        "GeoMaterialConfidence",
    ):
        folded = value.casefold()
        if folded == "certain" and value != "certain":
            result.update(
                action="NORMALIZE_VALUE",
                term="certain",
                reason="Normalize capitalization of standard confidence value.",
            )
        elif folded == "questionable" and value != "questionable":
            result.update(
                action="NORMALIZE_VALUE",
                term="questionable",
                reason="Normalize capitalization of standard confidence value.",
            )
        elif folded == "uncertain" or re.fullmatch(r"[-+]?\d+(\.\d+)?", value):
            result.update(
                action="REVIEW",
                term="",
                reason="Review whether this should be certain, questionable, or another defined confidence term.",
            )
    return result


def glossary_terms(arcpy, gdb):
    table = os.path.join(gdb, "Glossary")
    if not arcpy.Exists(table):
        return set()
    fields = {field.name for field in arcpy.ListFields(table)}
    if "Term" not in fields:
        return set()
    with arcpy.da.SearchCursor(table, ["Term"]) as cursor:
        return {display_value(row[0]) for row in cursor if display_value(row[0])}


def iter_datasets(arcpy, gdb):
    for dirpath, _, filenames in arcpy.da.Walk(
        gdb, datatype=["FeatureClass", "Table"]
    ):
        for filename in filenames:
            catalog_path = os.path.join(dirpath, filename)
            desc = arcpy.Describe(catalog_path)
            if getattr(desc, "featureType", "") == "Annotation":
                continue
            yield catalog_path


def inventory_dataset(arcpy, gdb, catalog_path, include_gemsy):
    fields = {field.name: field for field in arcpy.ListFields(catalog_path)}
    controlled = []
    for field in fields.values():
        if field.type != "String":
            continue
        if field.name in DEFINED_TERM_FIELDS:
            controlled.append((field.name, "GeMS controlled field"))
        elif include_gemsy and field.name.casefold().endswith(GEMSY_SUFFIXES):
            controlled.append((field.name, "GeMS-like suffix field"))
    if not controlled:
        return []

    rel_path = os.path.relpath(catalog_path, gdb).replace("\\", "/")
    parts = rel_path.split("/")
    feature_dataset = parts[0] if len(parts) > 1 else ""
    dataset = parts[-1]
    rows = []

    for field_name, field_check in controlled:
        cursor_fields = [field_name]
        if field_name == "Type":
            cursor_fields.extend(
                name for name in LINE_COMPANION_FIELDS if name in fields
            )
        elif field_name == "GeoMaterialConfidence" and "GeoMaterial" in fields:
            cursor_fields.append("GeoMaterial")
        counts = Counter()
        companions = defaultdict(lambda: defaultdict(Counter))
        with arcpy.da.SearchCursor(catalog_path, cursor_fields) as cursor:
            for record in cursor:
                if field_name == "GeoMaterialConfidence" and len(record) > 1:
                    if not display_value(record[1]):
                        continue
                value = display_value(record[0])
                if not value:
                    continue
                counts[value] += 1
                if field_name == "Type":
                    for name, companion in zip(cursor_fields[1:], record[1:]):
                        companions[value][name][display_value(companion)] += 1
        for value, count in counts.items():
            rows.append(
                {
                    "Dataset_Path": rel_path,
                    "Dataset": dataset,
                    "Feature_Dataset": feature_dataset,
                    "Field": field_name,
                    "Field_Check": field_check,
                    "Current_Value": value,
                    "Feature_Count": count,
                    "Companions": companions[value],
                }
            )
    return rows


def build_rows(arcpy, gdb, include_defined, include_gemsy):
    defined = glossary_terms(arcpy, gdb)
    raw_rows = []
    for catalog_path in iter_datasets(arcpy, gdb):
        raw_rows.extend(inventory_dataset(arcpy, gdb, catalog_path, include_gemsy))

    case_variants = defaultdict(set)
    for row in raw_rows:
        case_variants[row["Current_Value"].casefold()].add(row["Current_Value"])

    output = []
    for row in raw_rows:
        value = row["Current_Value"]
        is_defined = value in defined
        if is_defined and not include_defined:
            continue
        suggestion = make_suggestion(row["Field"], value, case_variants)
        companions = row.pop("Companions")
        row.update(
            {
                "Glossary_Status": "DEFINED" if is_defined else "MISSING",
                "Companion_IsConcealed": summarize(companions["IsConcealed"]),
                "Companion_LocationConfidenceMeters": summarize(
                    companions["LocationConfidenceMeters"]
                ),
                "Companion_ExistenceConfidence": summarize(
                    companions["ExistenceConfidence"]
                ),
                "Companion_IdentityConfidence": summarize(
                    companions["IdentityConfidence"]
                ),
                "Suggested_Action": suggestion["action"],
                "Suggested_Canonical_Term": suggestion["term"],
                "Suggested_IsConcealed": suggestion["concealed"],
                "Suggested_LocationConfidenceMeters": suggestion["location"],
                "Suggested_ExistenceConfidence": suggestion["existence"],
                "Suggested_IdentityConfidence": suggestion["identity"],
                "Suggestion_Reason": suggestion["reason"],
                "Include_In_Cleanup": "",
                "Review_Decision": "",
                "Canonical_Term": "",
                "Definition": "",
                "DefinitionSourceID": "",
                "Review_Notes": "",
            }
        )
        output.append(row)
    return sorted(
        output,
        key=lambda row: (
            row["Feature_Dataset"].casefold(),
            row["Dataset"].casefold(),
            row["Field"].casefold(),
            row["Current_Value"].casefold(),
            row["Current_Value"],
        ),
    )


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gdb", help="Path to the file geodatabase")
    parser.add_argument(
        "output_csv",
        nargs="?",
        help="Output CSV path; defaults beside the geodatabase",
    )
    parser.add_argument(
        "--include-defined",
        action="store_true",
        help="Include values that already have exact Glossary.Term entries.",
    )
    parser.add_argument(
        "--no-gemsy-fields",
        action="store_true",
        help="Skip nonstandard string fields ending in Type, Method, or Confidence.",
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
    if args.output_csv:
        output_csv = Path(args.output_csv)
    else:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        output_csv = Path(gdb).with_name(f"Glossary_inventory_{stamp}.csv")

    rows = build_rows(
        arcpy,
        gdb,
        include_defined=args.include_defined,
        include_gemsy=not args.no_gemsy_fields,
    )
    write_csv(output_csv, rows)

    message(arcpy, f"Glossary inventory written to: {output_csv}")
    message(arcpy, f"Review rows: {len(rows)}")
    message(
        arcpy,
        f"Distinct missing terms: {len({row['Current_Value'] for row in rows if row['Glossary_Status'] == 'MISSING'})}",
    )
    if not rows:
        warning(arcpy, "No glossary-controlled values matched the selected options.")
    message(arcpy, "No geodatabase values were changed.")
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
