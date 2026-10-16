"""Single-physical-line TSV rows; multiline quoted cells fail explicitly."""
import csv
import io
import re

from .contracts import fail, validate_text


def rows(script):
    result = []
    for line in re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", script):
        if not line:
            continue
        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else "\r" if line.endswith("\r") else ""
        raw = line[:-len(ending)] if ending else line
        try:
            cells = next(csv.reader([raw], delimiter="\t", strict=True))
        except (csv.Error, StopIteration):
            fail("TABLE_ROW_UNSUPPORTED")
        result.append((cells, ending, line))
    return result


def cell(script, row, column):
    if type(row) is not int or type(column) is not int or row < 0 or column < 0:
        fail("TABLE_LOCATION_INVALID")
    try:
        return rows(script)[row][0][column]
    except IndexError:
        fail("TABLE_LOCATION_INVALID")


def rewrite(script, changes):
    parsed = rows(script)
    touched = set()
    locations = set()
    for change in changes:
        row, column = change["field"][2:]
        if (row, column) in locations:
            fail("DUPLICATE_LOCATION")
        locations.add((row, column))
        if cell(script, row, column) != change["source"]:
            fail("TABLE_SOURCE_MISMATCH")
        validate_text(change["source"], change["target"])
        if any(c in change["target"] for c in "\r\n"):
            fail("TABLE_MULTILINE_UNSUPPORTED")
        parsed[row][0][column] = change["target"]
        touched.add(row)
    output = []
    for index, (cells, ending, original) in enumerate(parsed):
        if index not in touched:
            output.append(original)
            continue
        buffer = io.StringIO()
        csv.writer(buffer, delimiter="\t", lineterminator="", quoting=csv.QUOTE_MINIMAL).writerow(cells)
        output.append(buffer.getvalue() + ending)
    result = "".join(output)
    if len(rows(result)) != len(parsed):
        fail("TABLE_ROUNDTRIP_INVALID")
    for change in changes:
        if cell(result, *change["field"][2:]) != change["target"]:
            fail("TABLE_ROUNDTRIP_INVALID")
    return result
