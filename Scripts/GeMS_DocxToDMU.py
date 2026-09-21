"""
Reads contents of a Description Of Map Units Microsoft Word document formatted according to 
USGS Pubs template MapManuscript_v3-1_06-22.dotx, calculates HierarchyKey, and partially fills 
in a GeMS-style DescriptionOfMapUnits table. If the workspace does not have a 
DescriptionOfMapUnits table, one will be created. HierarchyKey values are calculated 
based on paragraph styling and indentation. If a table already exists, the tool will 
attempt to match updates with existing rows.

Arguments
    manuscript_file (str) : Path to the Word document
    gdb (str) : Path to database workspace
    zero_pad (int) : Length of each segment of HierarchyKey. All segments less than this 
        number will be padded to the left with zeros. It is best practice to always pad to 
        at least 3 spaces (the default) to try to avoid the value ever being interpreted as a 
        date datatype.
    arc_label (boolean) : Convert character styling of the unit label in the Word doc into 
        ArcGIS text formatting tags and saved in Label. Optional. False by default.
    html_label (boolean) : Convert character styling of the unit label in the Word doc into 
        HTML formatting tags and saved in Label. Optional. False by default.
    html_description (boolean) : Convert character styling of the unit description in the 
        Word doc into HTML formatting tags and saved in Description. Optional. False by default.

Dependencies
    docx (https://python-docx.readthedocs.io/en/latest/) included with toolbox in folder
        Scripts\docx
"""

import sys
import re
from pathlib import Path
import copy
import arcpy
import GeMS_utilityFunctions as guf
import docx

versionString = "GeMS_DocxToDMU.py, version of 10/24/24"
rawurl = "https://raw.githubusercontent.com/DOI-USGS/gems-tools-pro/master/Scripts/GeMS_DOCXToDMU.py"
guf.checkVersion(versionString, rawurl, "gems-tools-pro")

debug = False

# styles from MapManuscript_v3-1_06-22.dotx
style_dict = {
    "DMU-Heading1": "heading",
    "DMU-Heading2": "heading",
    "DMU-Heading3": "heading",
    "DMU-Heading4": "heading",
    "DMU-Heading5": "heading",
    "DMU Headnote - 1 Line": "headnote",
    "DMU Headnote - More Than 1 Line": "headnote",
    "DMU Headnote Paragraph": "headnote",
    "Run-inHead": "headnote",
    "DMU NoIndent": "unit text",
    "DMU Paragraph": "unit text",
    "DMU Quotation": "unit text",
    "DMU Unit 1 (1st after heading)": "unit",
    "DMU Unit 1": "unit",
    "DMU Unit 2": "unit",
    "DMU Unit 3": "unit",
    "DMU Unit 4": "unit",
    "DMU Unit 5": "unit",
    "DMU - List Bullet": "unit text",
    "DMU Unit Label (type style)": "label",
    "DMU Unit Name/Age (type style)": "age",
}


def strip_string(a):
    """Strip leading and trailing spaces from a string but check first
    that the string variable is not None."""
    if a:
        return str(a).strip()
    else:
        return a


def parse_text(p_object, doc_list, arc_label, html_label, html_description):
    mu = None
    label = None
    name = None
    age = None
    description = None

    style = p_object.style.name
    text = p_object.text

    # parse the text
    # heading text goes into Name
    if style_dict.get(style) == "heading":
        name = text

    # headnote text goes into Description
    elif style_dict.get(style) == "headnote":
        if html_description:
            description = apply_formatting(p_object.runs)
        else:
            description = text

    # only DMU unit paragraph style names are 10 characters long and end in a number
    elif (
        len(style) == 10
        and style[-1].isnumeric()
        or style == "DMU Unit 1 (1st after heading)"
    ):
        mu, i = text_runs(p_object, "DMU Unit Label (type style)")

        if arc_label:
            label = apply_formatting(p_object.runs[0 : i + 1], "arc")
        elif html_label:
            label = apply_formatting(p_object.runs[0 : i + 1], "html")
        else:
            label = mu

        name_age, i = text_runs(p_object, "DMU Unit Name/Age (type style)")
        if not mu or not name_age:
            raise ValueError(
                f"Unit paragraph is missing its label or name character style: {text[:80]!r}"
            )

        # if there are parantheses, we have an age,
        # partition on the first one to get the unit name
        if re.search(r"\((?:18|19|20)\d{2}\)$", name_age.strip()):
            name = name_age
            age = None
        elif name_age.find("(") > 0:
            name, x, age = name_age.partition("(")
            # and strip the final ")" character
            age = age.rstrip(")")
        else:
            # there is only a name
            age = None
            name = name_age

        if html_description:
            description = apply_formatting(p_object.runs[i + 1 :])
        else:
            d_list = []
            for r in p_object.runs[i + 1 :]:
                d_list.append(r.text)
            description = "".join(d_list)

        # remove the printed divider between a unit name and its description
        description = description.lstrip(" \t—–-")

    else:
        print(f"Unknown paragraph style '{style}'")

    # check all column values to leading and trailing spaces
    cols = [mu, label, name, age, description]
    vals = list(map(strip_string, cols))
    vals.append(doc_list)

    return tuple(vals)


def text_runs(p, style_name):
    # build a single text string from the text of sequential runs
    # that all share the same character style
    text_runs = []
    i = 0
    for n, run in enumerate(p.runs):
        if run.style.name == style_name:
            i = n
            text_runs.append(run.text)

    return "".join(text_runs), i


def child_hkey(hkey):
    this_hkey = copy.deepcopy(hkey)
    this_hkey.append(1)
    return this_hkey


def sibling_hkey(hkey):
    # increment the last item in the previous hkey
    last_element = int(copy.copy(hkey[-1])) + 1
    # remove the last item in the previous hkey
    this_key = hkey[0:-1]
    # append the incremented item
    this_key.append(last_element)

    return this_key


class HierarchyBuilder:
    """Assign unique keys from the explicit heading and unit style levels."""

    def __init__(self):
        self.child_counts = {}
        self.headings = {}
        self.units = {}

    def _child(self, parent):
        parent = tuple(parent)
        self.child_counts[parent] = self.child_counts.get(parent, 0) + 1
        return list(parent) + [self.child_counts[parent]]

    def add(self, style):
        kind = style_dict[style]
        if kind == "heading":
            level = int(style[-1])
            if level > 1 and level - 1 not in self.headings:
                raise ValueError(f"{style} has no parent heading")
            parent = self.headings.get(level - 1, [])
            key = self._child(parent)
            self.headings = {k: v for k, v in self.headings.items() if k < level}
            self.headings[level] = key
            self.units.clear()
            return key
        if kind == "unit":
            level = 1 if style == "DMU Unit 1 (1st after heading)" else int(style[-1])
            if level == 1:
                if not self.headings:
                    raise ValueError(f"{style} has no parent heading")
                parent = self.headings[max(self.headings)]
            else:
                if level - 1 not in self.units:
                    raise ValueError(f"{style} has no parent unit")
                parent = self.units[level - 1]
            key = self._child(parent)
            self.units = {k: v for k, v in self.units.items() if k < level}
            self.units[level] = key
            return key
        if kind == "headnote":
            if not self.headings:
                raise ValueError(f"{style} has no parent heading")
            return self._child(self.headings[max(self.headings)])
        raise ValueError(f"{style} does not create a DMU row")


def para_props(p_style):
    """return a general paragraph style type based on style_dict
    and, for unit and heading paragraphs, a rank"""
    p_type = style_dict.get(p_style)

    if p_type in ("unit", "heading"):
        if p_style == "DMU Unit 1 (1st after heading)":
            rank = "1"
        else:
            rank = p_style[-1]

    if p_type in ("unit text", "headnote"):
        rank = 0

    return p_type, rank


def run_props(run, label_special="None"):
    unit_label = False
    r_dict = {}
    if run.style.name == "DMU Unit Label (type style)":
        unit_label = True
        if label_special == "arc":
            r_dict["fnt"] = "<fnt name='FGDCGeoAge'>"
        else:
            r_dict["span"] = f'<span style="font-family: {run.font.name};">'

    if run.font.name and not unit_label:
        r_dict["span"] = f'<span style="font-family: {run.font.name};">'

    if run.style.name == "DMU Unit Name/Age (type style)":
        r_dict["strong"] = "<strong>"

    if run.font.bold and not run.style.name == "DMU Unit Name/Age (type style)":
        if unit_label:
            if label_special == "arc":
                r_dict["bol"] = "<bol>"
            else:
                r_dict["strong"] = "<strong>"

    if run.style.name == "Run-inHead":
        r_dict["em"] = "<em>"

    if run.font.italic:
        if unit_label:
            r_dict["ita"] = "<ita>"
        else:
            r_dict["em"] = "<em>"

    if run.font.superscript:
        r_dict["sup"] = "<sup>"
    if run.font.subscript:
        r_dict["sub"] = "<sub>"

    return r_dict


def apply_formatting(runs, label_special="None"):
    p_contents = []

    for i, run in enumerate(runs):
        current_run = run_props(run, label_special)
        if i == 0:
            if current_run.get("fnt"):
                p_contents.append(current_run["fnt"])

            if current_run.get("span"):
                p_contents.append(current_run["span"])

            for k in [n for n in current_run.keys() if not n in ("fnt", "span")]:
                p_contents.append(current_run[k])

        else:
            # this is not the first run and we have to check the previous one for how it was formatted
            previous_run = run_props(runs[i - 1], label_special)

            # if any tag in the last run is not in the current run
            # then close it
            prev_tags = list(previous_run.keys())
            prev_tags.reverse()
            for tag in prev_tags:
                if not tag in current_run.keys():
                    if not tag == "span" and label_special == "arc":
                        p_contents.append(f"</{tag}>")

            for tag in [k for k in current_run.keys() if not k in previous_run.keys()]:
                p_contents.append(current_run[tag])

        p_contents.append(run.text)

        if i == len(runs) - 1:
            curr_tags = list(current_run.keys())
            curr_tags.reverse()
            for tag in curr_tags:
                p_contents.append(f"</{tag}>")

    p_text = "".join(p_contents)

    return p_text


def replace_formatting(runs):
    """
    Detects formatting runs in a paragraph.

    Returns a string where Word formatted bold, italic, super/subscript and
    cases of FGDCGeoAge have been converted to HTML formatting tags
    (mostly written by ChatGPT!)
    """
    formatting_runs = []
    current_run_text = ""
    current_run_formatting = {}

    for run in runs:
        # if run.text:
        # Check if formatting properties have changed
        if (
            run.bold != current_run_formatting.get("bold")
            or run.italic != current_run_formatting.get("italic")
            or run.font.name != current_run_formatting.get("font")
            or run.style.name != current_run_formatting.get("style")
            or run.font.superscript != current_run_formatting.get("super")
            or run.font.subscript != current_run_formatting.get("sub")
        ):
            if current_run_text:
                formatting_runs.append((current_run_text, current_run_formatting))
            current_run_text = run.text
            current_run_formatting = {
                "bold": run.bold,
                "italic": run.italic,
                "font": run.font.name,
                "style": run.style.name,
                "super": run.font.superscript,
                "sub": run.font.subscript,
            }
        else:
            current_run_text += run.text

    # Append the last run
    if current_run_text:
        formatting_runs.append((current_run_text, current_run_formatting))

    reformatted = []
    for text, formatting in formatting_runs:
        if formatting.get("bold"):
            text = f"<b>{text}</b>"
        if formatting.get("italic"):
            text = f"<em>{text}</em>"
        if (
            formatting.get("font") == "FGDCGeoAge"
            or formatting.get("style") == "DMU Unit Label (type style)"
        ):
            text = f'<span style="font-family: FGDCGeoAge;">{text}</span>'
        if formatting.get("style") == "Run-inHead":
            text = f"<em>{text}</em>"
        if formatting.get("super") == True:
            text = f"<sup>{text}</sup>"
        if formatting.get("sub") == True:
            text = f"<sub>{text}</sub>"

        reformatted.append(text)

    return "".join(reformatted)


def get_length(f_name, dmu_table):
    f = arcpy.ListFields(dmu_table, f_name)
    return f[0].length


def alter_table(doc_list, dmu_table):
    """attempts to increase the length property of a field in the DMU table if the
    corresponding text from the Word document is too long"""
    # get a list of the lengths of all items in doc_list
    # doc_list = [hkey, paragraph_style, mu, label, name, age, desc]
    field_props = {
        "hierarchykey": (0, get_length("hierarchykey", dmu_table)),
        "paragraphstyle": (1, get_length("paragraphstyle", dmu_table)),
        "mapunit": (2, get_length("mapunit", dmu_table)),
        "label": (3, get_length("label", dmu_table)),
        "name": (4, get_length("name", dmu_table)),
        "age": (5, get_length("age", dmu_table)),
        "description": (6, get_length("description", dmu_table)),
    }

    maxs = []
    for j in range(0, 7):
        max_j = max([len(p[j]) if p[j] else 0 for p in doc_list])
        maxs.append(max_j)

    for k, v in field_props.items():
        if v[1] < maxs[v[0]]:
            try:
                arcpy.AddWarning(f"Trying to increase the length of {k}.")
                arcpy.AlterField_management(dmu_table, k, field_length=maxs[v[0]])
            except:
                arcpy.AddError(
                    f"""Failed to increase the length of {k} to at least {maxs[v[0]]} to accommodate long text in the Word document.
                               Try altering it manually before trying again."""
                )


def main(params):
    """Parses a Word document of a DMU into a DMU table

    Args:
        manuscript_file (file path): MS Word document that contains only a Description of Map Units section
            based on USGS MapManuscript_v3-1_06-22.dotx.
        gdb (file path): File geodatabase that may or may not contain a DescriptionOfMapUnits table
        zero_pad (integer): How many spaces left of the number should be filled with zeros
        reformat (boolean): Attempt (or not) to translate Word character styles in descriptions to HTML format tags
    """
    manuscript_file = params[0]
    gdb = params[1]

    zero_pad = 3
    if len(params) > 2:
        zero_pad = int(params[2])

    arc_label = False
    if len(params) > 3:
        arc_label = guf.eval_bool(params[3])

    html_label = False
    if len(params) > 4:
        html_label = guf.eval_bool(params[4])

    html_description = False
    if len(params) > 5:
        html_description = guf.eval_bool(params[5])

    guf.addMsgAndPrint(versionString)

    guf.addMsgAndPrint(f"Parsing file {manuscript_file}")

    # open document and get a list of paragraphs
    document = docx.Document(manuscript_file)
    paras = document.paragraphs

    # The converter accepts a DMU-only document, optionally preceded by its title.
    if paras and paras[0].text.strip().lower() == "description of map units":
        paras = paras[1:]
    paras = [p for p in paras if p.text.strip()]
    if not paras:
        raise ValueError("No DMU paragraphs found in the manuscript")

    hierarchy = HierarchyBuilder()
    doc_list = []
    for p in paras:
        style = p.style.name
        kind = style_dict.get(style)
        if kind is None:
            raise ValueError(f"Unknown DMU paragraph style: {style!r} ({p.text[:40]!r})")
        if kind == "unit text":
            if not doc_list:
                raise ValueError("DMU continuation paragraph has no preceding row")
            paragraph = apply_formatting(p.runs) if html_description else p.text
            doc_list[-1][6] = f"{doc_list[-1][6]}\n{paragraph}"
            continue
        hkey = hierarchy.add(style)
        mu, label, name, age, description, _ = parse_text(
            p, doc_list, arc_label, html_label, html_description
        )
        doc_list.append([hkey, style, mu, label, name, age, description])

    for line in doc_list:
        hkey_list = [str(i).zfill(zero_pad) for i in line[0]]
        line[0] = "-".join(hkey_list)

    dmu_table = str(Path(gdb) / "DescriptionOfMapUnits")
    if not arcpy.Exists(dmu_table):
        try:
            import GeMS_ALaCarte as gacl

            vt = arcpy.ValueTable(3)
            vt.loadFromString("# # DescriptionOfMapUnits")
            gacl.process(gdb, vt)
        except Exception as exc:
            raise RuntimeError(
                f"DescriptionOfMapUnits table could not be found and could not be created in {gdb}"
            ) from exc

    # for line in doc_list:
    #     arcpy.AddMessage(line)
    # sys.exit()

    # check if we have to increase the lengths of any fields to accommodate the document text
    alter_table(doc_list, dmu_table)

    guf.addMsgAndPrint("Document parsed.")
    guf.addMsgAndPrint(f"Searching for changes to be made to {dmu_table}")

    cursor_fields = [
        "HierarchyKey",
        "ParagraphStyle",
        "MapUnit",
        "label",
        "Name",
        "Age",
        "Description",
    ]

    table_list = [list(row) for row in arcpy.da.SearchCursor(dmu_table, cursor_fields)]

    # eval_list is every row in the document that does not have an EXACT match in the table list
    # we know these are different, but do they need to update an only partially different row
    # or are they brand new and need to be inserted? Each action requires a different cursor
    mod_list = [row for row in doc_list if not row in table_list]
    # for row in mod_list:
    #     arcpy.AddMessage(row)

    insert_list = []
    update_list = []
    for row in mod_list:
        update_row = False
        hkey = row[0]
        mu = row[2]
        label = row[3]
        name = row[4]
        description = row[6]

        # add double quotes to prepare for SQL query
        if name:
            name = name.replace("'", "''")
        if description:
            description = description.replace("'", "''")

        wheres = [
            f"MapUnit = '{mu}' AND MapUnit IS NOT NULL",  # MapUnit is primary key
            f"MapUnit IS NOT NULL AND MapUnit <> '{mu}' AND Name = '{name}'",  # Name is primary key when MapUnits are not the same
            f"MapUnit IS NULL AND Name = '{name}'",  # Name is primary key when MapUnit is null (heading)
            # f"MapUnit IS NULL AND Name IS NOT NULL AND Name <> '{name}' AND HierarchyKey = '{hkey}'",  # heading has not been changed but not re-ordered
            # f"MapUnit IS NULL AND Name IS NULL and Description <> '{description}' AND HierarchyKey = '{hkey}'",  # description (headnote) is primary key
            f"MapUnit IS NULL AND Name IS NULL and Description = '{description}'",  # Description (headnote when MapUnit and Name are NULL) is primary key
        ]

        for where in wheres:
            updates = [
                row
                for row in arcpy.da.SearchCursor(
                    dmu_table, cursor_fields, where_clause=where
                )
            ]
            if len(updates) > 0:
                row.append(where)
                update_list.append(row)
                update_row = True
                break

        if not update_row:
            insert_list.append(row)

    # arcpy.AddMessage("update_list")
    # for row in update_list:
    #     arcpy.AddMessage(row)

    # arcpy.AddMessage("insert_list")
    # for row in insert_list:
    #     arcpy.AddMessage(row)

    # sys.exit()

    if update_list:
        guf.addMsgAndPrint(f"{len(update_list)} row(s) will be updated.")
        for r in update_list:
            where = r[7]
            vals = r[0:7]
            with arcpy.da.UpdateCursor(
                dmu_table, cursor_fields, where_clause=where
            ) as cursor:
                for row in cursor:
                    cursor.updateRow(vals)

    if insert_list:
        guf.addMsgAndPrint(f"{len(insert_list)} new row(s) will be inserted.")
        with arcpy.da.InsertCursor(dmu_table, cursor_fields) as cursor:
            for r in insert_list:
                cursor.insertRow(r)

    if not update_list and not insert_list:
        guf.addMsgAndPrint(
            "Document content matches table content. No changes will be made."
        )

    # finally, check for duplicate HierarchyKeys. This is most likely to happen when a headnote description has changed.
    # In this case, none of the SQL queries above will yeild results and the item in mod_list will marked for insertion
    # instead of updating.
    with arcpy.da.SearchCursor(dmu_table, ("OID@", "HierarchyKey")) as cursor:
        hk_dict = {}
        for row in cursor:
            if not row[1] in hk_dict:
                hk_dict[row[1]] = [row[0]]
            else:
                oids = hk_dict[row[1]]
                oids.append(row[0])
                hk_dict[row[1]] = oids

    dups = {k: v for k, v in hk_dict.items() if len(v) > 1}

    if dups:
        arcpy.AddWarning(
            "Identical HierarchyKeys found! This usually occurs when the text for a headnote"
            "has been changed. The tool cannot determine if the edited information is to be used to update"
            "an existing headnote or not and so it is marked for insertion. Review the following HierarchyKey"
            "values and delete the ones you don't want:"
        )
        for k, v in dups.items():
            arcpy.AddWarning(f"{k}: OBJECTIDs {', '.join([str(n) for n in v])}")


if __name__ == "__main__":
    main(sys.argv[1:])
