"""Inventory every populated *SourceID field in a GeMS geodatabase.

The script uses the reviewed ContactsAndFaults source crosswalk as a seed,
suggests canonical DataSources_ID values for equivalent legacy spellings, and
writes a CSV for review. It does not modify the geodatabase.
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


BASE_REQUIRED = {
    "SourceID_As_Entered",
    "Canonical_SourceID",
}
OUTPUT_FIELDS = (
    "Dataset_Path",
    "Dataset",
    "Feature_Dataset",
    "Field",
    "Current_SourceID",
    "Feature_Count",
    "Status",
    "Suggested_Canonical_SourceID",
    "Match_Method",
    "Action",
    "Canonical_SourceID",
    "Review_Notes",
)
DAS_RE = re.compile(r"^DAS[1-9]\d*$")
YEAR_RE = re.compile(r"(?<!\d)(?:18|19|20)\d{2}[a-z]?(?!\d)", re.I)


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
    return "" if value is None else str(value).strip()


def normalize(value):
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"\band\s+others\b", " etal ", value)
    value = re.sub(r"\bet\s*al\.?\b", " etal ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def first_name(value):
    normalized = normalize(value)
    return normalized.split()[0] if normalized else ""


def years(value):
    return tuple(match.casefold() for match in YEAR_RE.findall(value))


def read_base_crosswalk(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = BASE_REQUIRED.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(
                "Base crosswalk is missing columns: " + ", ".join(sorted(missing))
            )
        rows = list(reader)

    exact = {}
    normalized = defaultdict(set)
    surname = defaultdict(set)
    surname_year = defaultdict(set)
    for line_number, row in enumerate(rows, start=2):
        raw = text(row["SourceID_As_Entered"])
        canonical = text(row["Canonical_SourceID"])
        if not raw or raw == "<NULL>" or not canonical:
            continue
        if not DAS_RE.fullmatch(canonical):
            raise ValueError(f"Line {line_number}: invalid canonical ID {canonical!r}")
        if raw in exact and exact[raw] != canonical:
            raise ValueError(f"Conflicting base mappings for {raw!r}")
        exact[raw] = canonical
        normalized[normalize(raw)].add(canonical)
        name = first_name(raw)
        surname[name].add(canonical)
        for year in years(raw):
            surname_year[(name, year)].add(canonical)
    return exact, normalized, surname, surname_year


def existing_sources(arcpy, gdb):
    table = os.path.join(gdb, "DataSources")
    if not arcpy.Exists(table):
        raise ValueError("DataSources table does not exist")
    with arcpy.da.SearchCursor(table, ["DataSources_ID"]) as cursor:
        return {text(row[0]) for row in cursor if text(row[0])}


def manual_alias(value, exact):
    n = normalize(value)
    das1 = exact.get("This study")
    if n in {"this map", "this report", "this study"} and das1:
        return das1, "project-source alias"

    if re.search(r"\brami(?:rez|erz|erez|eriz|rz)\b", n):
        candidates = {
            canonical
            for raw, canonical in exact.items()
            if first_name(raw).startswith("rami")
        }
        if len(candidates) == 1:
            return candidates.pop(), "Ramirez spelling alias"

    if re.match(r"mclau?ghlin\b", n) and not any(
        year in n for year in ("2004", "2018")
    ):
        unpublished_markers = (
            "unpub",
            "asti",
            "geyser",
            "highland springs",
            "kelseyville",
            "mtsthelena",
            "mt st helena",
            "st helena",
            "whispering pines",
        )
        if any(marker in n for marker in unpublished_markers):
            candidates = {
                canonical
                for raw, canonical in exact.items()
                if canonical and "mclaughlin" in normalize(raw) and not years(raw)
            }
            if len(candidates) == 1:
                return candidates.pop(), "unpublished McLaughlin mapping alias"
    return "", ""


def suggest(value, canonical_ids, indexes):
    exact, normalized, surname, surname_year = indexes
    if value in canonical_ids:
        return value, "existing DataSources_ID", "CANONICAL", "KEEP"
    if value in exact:
        return exact[value], "exact reviewed crosswalk", "LEGACY_MAPPED", "APPLY"

    candidates = normalized.get(normalize(value), set())
    if len(candidates) == 1:
        return candidates.copy().pop(), "normalized reviewed identifier", "LEGACY_MAPPED", "APPLY"

    name = first_name(value)
    value_years = years(value)
    year_matches = set()
    for year in value_years:
        year_matches.update(surname_year.get((name, year), set()))
    if len(year_matches) == 1:
        return year_matches.pop(), "author and year", "SUGGESTED_ALIAS", "REVIEW"

    if len(surname.get(name, set())) == 1:
        return surname[name].copy().pop(), "unique author in reviewed crosswalk", "SUGGESTED_ALIAS", "REVIEW"

    alias, method = manual_alias(value, exact)
    if alias:
        return alias, method, "SUGGESTED_ALIAS", "REVIEW"
    return "", "no unique match", "UNRESOLVED", "REVIEW"


def iter_source_fields(arcpy, gdb):
    for dirpath, _, filenames in arcpy.da.Walk(
        gdb, datatype=["FeatureClass", "Table"]
    ):
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            desc = arcpy.Describe(path)
            if getattr(desc, "featureType", "") == "Annotation":
                continue
            source_fields = [
                field.name
                for field in arcpy.ListFields(path)
                if field.type == "String" and field.name.casefold().endswith("sourceid")
            ]
            if source_fields:
                rel = os.path.relpath(path, gdb).replace("\\", "/")
                yield rel, path, source_fields


def build_inventory(arcpy, gdb, indexes):
    canonical_ids = existing_sources(arcpy, gdb)
    output = []
    for rel, path, fields in iter_source_fields(arcpy, gdb):
        parts = rel.split("/")
        feature_dataset = parts[0] if len(parts) > 1 else ""
        dataset = parts[-1]
        for field in fields:
            counts = Counter()
            with arcpy.da.SearchCursor(path, [field]) as cursor:
                for (value,) in cursor:
                    value = text(value)
                    if value:
                        counts[value] += 1
            for value, count in counts.items():
                canonical, method, status, action = suggest(value, canonical_ids, indexes)
                output.append(
                    {
                        "Dataset_Path": rel,
                        "Dataset": dataset,
                        "Feature_Dataset": feature_dataset,
                        "Field": field,
                        "Current_SourceID": value,
                        "Feature_Count": count,
                        "Status": status,
                        "Suggested_Canonical_SourceID": canonical,
                        "Match_Method": method,
                        "Action": action,
                        "Canonical_SourceID": "",
                        "Review_Notes": "",
                    }
                )
    return sorted(
        output,
        key=lambda row: (
            row["Feature_Dataset"].casefold(),
            row["Dataset"].casefold(),
            row["Field"].casefold(),
            row["Current_SourceID"].casefold(),
        ),
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gdb", help="Path to the file geodatabase")
    parser.add_argument("base_crosswalk", help="Reviewed ContactsAndFaults source CSV")
    parser.add_argument("output_csv", nargs="?", help="Output review CSV")
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
    indexes = read_base_crosswalk(args.base_crosswalk)
    rows = build_inventory(arcpy, gdb, indexes)
    if args.output_csv:
        output = Path(args.output_csv)
    else:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        output = Path(gdb).with_name(f"All_DataSourceID_inventory_{stamp}.csv")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    message(arcpy, f"DataSourceID inventory written to: {output}")
    message(arcpy, f"Inventory rows: {len(rows)}")
    message(arcpy, f"Unresolved rows: {sum(row['Status'] == 'UNRESOLVED' for row in rows)}")
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
