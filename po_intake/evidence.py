import re
from dataclasses import dataclass


HEADERS = {
    "po_number": r"(?:po|purchase order)\s*(?:number|no\.?|#)",
    "customer_reference": r"customer\s*(?:reference|ref\.?|code|id)",
    "currency": r"currency",
}
LINE_FIELDS = ("sku", "quantity", "unit_price")
COLUMNS = {
    "sku": "sku", "item code": "sku", "product code": "sku",
    "qty": "quantity", "quantity": "quantity",
    "unit price": "unit_price", "unit price (myr)": "unit_price", "unit price (rm)": "unit_price",
    "unit": "unit", "uom": "unit", "unit of measure": "unit",
}
CELL = re.compile(r"\S.*?(?=\s{2,}|\t|\s*$)")
PIPE_CELL = re.compile(r"[^|\s][^|]*?(?=\s*(?:\||$))")
SKU_TOKEN = re.compile(r"[\w][\w./-]*")
NUMERIC_CELL = re.compile(r"(?:[A-Za-z]{2,3}\s*)?\d[\d,.]*")


def normalize(value):
    return " ".join(value.split())


@dataclass(frozen=True)
class Cell:
    value: str
    start: int
    end: int


@dataclass
class SourcePage:
    number: int
    text: str
    headers: dict
    rows: dict
    issues: list


def source_pages(pages):
    for page in pages or []:
        headers = {field: [] for field in HEADERS}
        rows = {}
        issues = []
        offset = 0
        columns = None
        for number, original in enumerate(page["text"].splitlines(keepends=True), 1):
            line = original.rstrip("\r\n")
            for field, label in HEADERS.items():
                match = re.fullmatch(rf"\s*{label}\s*[:=]\s*(\S.*?)\s*", line, re.I)
                if match:
                    headers[field].append(Cell(match[1], offset + match.start(1), offset + match.end(1)))

            cells_in_line = PIPE_CELL if "|" in line else CELL
            matches = list(cells_in_line.finditer(line))
            labels = [COLUMNS.get(normalize(match[0]).casefold()) for match in matches]
            if all(labels.count(name) == 1 for name in LINE_FIELDS):
                columns = labels
            elif columns and len(matches) == len(columns):
                cells = {
                    field: Cell(match[0], offset + match.start(), offset + match.end())
                    for field, match in zip(columns, matches) if field
                }
                if SKU_TOKEN.fullmatch(cells["sku"].value):
                    rows[(page["page"], number)] = cells
            elif columns and matches and SKU_TOKEN.fullmatch(matches[0][0]) and any(
                NUMERIC_CELL.fullmatch(match[0]) for match in matches[1:]
            ):
                issues.append(f"Page {page['page']}, text line {number}: Possible order row has inconsistent columns.")
            offset += len(original)
        yield SourcePage(page["page"], page["text"], headers, rows, issues)


def quote_location(text, excerpt):
    if not excerpt or not normalize(excerpt):
        return None, "Provide an excerpt from the stated page."
    tokens = list(re.finditer(r"\S+", text))
    normalized = " ".join(token[0] for token in tokens)
    quote = normalize(excerpt)
    start = normalized.find(quote)
    if start < 0:
        return None, "The quoted text is absent from the stated page."
    if normalized.find(quote, start + 1) >= 0:
        return None, "The quote repeats on the page. Select a more specific excerpt."
    positions = []
    for token in tokens:
        if positions:
            positions.append(token.start() - 1)
        positions.extend(range(token.start(), token.end()))
    return (positions[start], positions[start + len(quote) - 1] + 1), None


def same_value(field, proposed, source):
    if field == "currency":
        return normalize(proposed).upper() == ("MYR" if normalize(source).upper() == "RM" else normalize(source).upper())
    return normalize(proposed) == normalize(source)


def check_field(field, proposal, pages, sku=None):
    result = {"page": proposal.page, "value": proposal.value, "status": "unverified"}
    if not proposal.value or not proposal.value.strip() or proposal.alternatives:
        return {**result, "issue": "A single value is required."}, None
    page = pages.get(proposal.page)
    if not page:
        return {**result, "issue": "Choose a valid source page."}, None
    location, issue = quote_location(page.text, proposal.excerpt)
    if issue:
        return {**result, "issue": issue}, None
    start, end = location
    headers = field in HEADERS
    if headers:
        candidates = [cell for source in pages.values() for cell in source.headers[field]]
        if len({normalize(cell.value) for cell in candidates}) > 1:
            return {**result, "issue": "Several labeled values appear in this document."}, None
        cells = [(None, cell) for cell in page.headers[field]]
    else:
        cells = [
            (row, data[field]) for row, data in page.rows.items()
            if field in data and (sku is None or same_value("sku", sku, data["sku"].value))
        ]
    matches = [(row, cell) for row, cell in cells if start <= cell.start and cell.end <= end
               and same_value(field, proposal.value, cell.value)]
    if len(matches) != 1:
        detail = "The quote does not identify this value in its labeled field."
        if not headers:
            detail = "The quote does not identify this value in the correct SKU row and column."
        return {**result, "start": start, "end": end, "issue": detail}, None
    row, _ = matches[0]
    return {**result, "status": "verified", "excerpt": page.text[start:end],
            "start": start, "end": end}, row


def verify(draft, pages):
    sources = {page.number: page for page in source_pages(pages)}
    fields = {name: check_field(name, getattr(draft, name), sources)[0] for name in HEADERS}
    issues = [f"{name.replace('_', ' ').capitalize()}: {entry['issue']}"
              for name, entry in fields.items() if entry["status"] != "verified"]
    line_fields, used = [], set()
    for position, line in enumerate(draft.lines, 1):
        findings = {}
        line_rows = set()
        sku = line.sku.value or ""
        for field in LINE_FIELDS:
            result, row = check_field(field, getattr(line, field), sources, sku=sku)
            findings[field] = result
            if row is not None:
                line_rows.add(row)
            else:
                issues.append(f"Line {position} {field.replace('_', ' ')}: {result['issue']}")
        if len(line_rows) > 1:
            issue = "The proposed fields point to different PDF rows."
            issues.append(f"Line {position}: {issue}")
            for entry in findings.values():
                if entry["status"] == "verified":
                    entry.update(status="unverified", issue=issue)
                    entry.pop("excerpt")
        elif len(line_rows) == 1 and all(entry["status"] == "verified" for entry in findings.values()):
            row = next(iter(line_rows))
            if row in used:
                issue = "This PDF row is already assigned to another draft line."
                issues.append(f"Line {position}: {issue}")
                for entry in findings.values():
                    entry.update(status="unverified", issue=issue)
                    entry.pop("excerpt")
            used.add(row)
            unit = sources[row[0]].rows[row].get("unit")
            if unit:
                findings["unit"] = {
                    "status": "verified", "page": row[0], "value": unit.value,
                    "excerpt": unit.value, "start": unit.start, "end": unit.end,
                }
                if line.unit and normalize(line.unit) != normalize(unit.value):
                    issues.append(f"Line {position}: Proposed unit differs from the PDF unit {unit.value!r}.")
        line_fields.append(findings)
    if any(row not in used for source in sources.values() for row in source.rows):
        issues.append("One or more PDF rows have no completely verified draft line.")
    issues.extend(f"Page {source.number}: No supported order table was found."
                  for source in sources.values() if source.text.strip() and not source.rows)
    issues.extend(issue for source in sources.values() for issue in source.issues)
    return {"fields": fields, "lines": line_fields, "issues": issues}


def supported_correction(draft, pages, field, proposal, line_number=None):
    sources = {page.number: page for page in source_pages(pages)}
    if line_number is None:
        return check_field(field, proposal, sources)[0]
    line = draft.lines[line_number - 1]
    if field == "unit":
        return check_field(field, proposal, sources, sku=line.sku.value or "")[0]
    sku = proposal.value if field == "sku" else line.sku.value
    return check_field(field, proposal, sources, sku=sku or "")[0]
