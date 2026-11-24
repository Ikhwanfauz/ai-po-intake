"""Build and query the local heading-based SOP index."""
import argparse
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

from po_intake.config import Settings


SOP_DIR = Path(__file__).resolve().parents[1] / "sops"
METHOD = "heading-term-idf-v1"
MIN_TERMS = 2
LIMIT = 3
ALIASES = {
    "sign off": "approve", "signing off": "approve", "green light": "approve",
    "go ahead": "approve", "part code": "sku", "part codes": "sku",
    "buyer account": "customer reference", "units ordered": "quantity",
    "unit costs": "unit price", "unit cost": "unit price", "purchase order": "po",
    "version": "revision", "versions": "revision", "quantities": "quantity",
    "approval": "approve", "approving": "approve", "approved": "approve",
    "checks": "check", "checking": "check", "verify": "check", "verification": "check",
    "prices": "price", "resends": "resend", "sent again": "resend", "revised": "revision",
    "revisions": "revision", "corrected": "correct", "corrections": "correct",
}
STOP = set(("a an and are as at be before by can do for from has have how i in is it its me of on or our "
            "should so that the their them then there this to under was we were what when where which who "
            "will with you your else now same next need really po pdf sop each against still many much "
            "does tell show know about ask says say after").split())
ALIAS_RE = re.compile(r"\b(?:" + "|".join(map(re.escape, sorted(ALIASES, key=len, reverse=True))) + r")\b")


def terms(text):
    normalized = ALIAS_RE.sub(lambda match: ALIASES[match[0]], text.lower())
    return {word for word in re.findall(r"[a-z]+", normalized) if word not in STOP}


def corpus(directory):
    paths = sorted(directory.glob("SOP-*.md"))
    if not paths:
        raise ValueError("The versioned SOP corpus is empty.")
    sources = [(path.name, path.read_bytes().replace(b"\r\n", b"\n")) for path in paths]
    digest = hashlib.sha256()
    for name, data in sources:
        digest.update(name.encode() + b"\0" + data + b"\0")
    return digest.hexdigest(), sources


def extract_sections(sources):
    for name, data in sources:
        document = re.fullmatch(r"(SOP-\d+)\.rev(\d+)\.md", name)
        if not document:
            raise ValueError(f"Invalid versioned SOP filename: {name}")
        pieces = re.split(r"^## (\d+)\. ([^\n]+)\n", data.decode("utf-8"), flags=re.M)
        if len(pieces) == 1:
            raise ValueError(f"No numbered SOP headings in {name}")
        for index in range(1, len(pieces), 3):
            section_id, heading, excerpt = pieces[index:index + 3]
            text = excerpt.strip()
            if not text:
                raise ValueError(f"Empty SOP section {section_id} in {name}")
            doc_id, revision = document.groups()
            yield {
                "key": f"{doc_id}.rev{revision}.{section_id}", "document_id": doc_id, "revision": int(revision),
                "heading": heading.strip(), "section_id": section_id, "excerpt": text,
            }


def build(directory, output):
    digest, sources = corpus(directory)
    sections = list(extract_sections(sources))
    if len({section["key"] for section in sections}) != len(sections):
        raise ValueError("SOP section IDs must be unique within a revision.")
    entries = [{**section, "heading_terms": sorted(terms(section["heading"])),
                "body_terms": sorted(terms(section["excerpt"]))} for section in sections]
    index = {"method": METHOD, "corpus_version": digest, "sections": entries}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return index


def load(path, directory=SOP_DIR):
    index = json.loads(path.read_text(encoding="utf-8"))
    digest, sources = corpus(directory)
    if index.get("method") != METHOD or index.get("corpus_version") != digest:
        raise ValueError("The SOP index is out of date. Rebuild it from the versioned sources.")
    source_sections = list(extract_sections(sources))
    entries = index.get("sections", [])
    if [{k: value for k, value in entry.items() if k not in {"heading_terms", "body_terms"}}
            for entry in entries] != source_sections:
        raise ValueError("Indexed SOP passages differ from their source documents.")
    return index


def search(index, question):
    query = terms(question)
    entries = index["sections"]
    frequencies = Counter()
    for entry in entries:
        frequencies.update(set(entry["heading_terms"]) | set(entry["body_terms"]))
    hits = []
    for entry in entries:
        title = query.intersection(entry["heading_terms"])
        body = query.intersection(entry["body_terms"])
        if len(body | title) < MIN_TERMS:
            continue
        score = sum((3 if word in title else 1) *
                    (1 + math.log((len(entries) + 1) / (frequencies[word] + 1))) for word in title | body)
        hits.append({k: value for k, value in entry.items() if k not in {"heading_terms", "body_terms"}} |
                    {"score": round(score, 4), "matched_terms": sorted(title | body)})
    return sorted(hits, key=lambda hit: (-hit["score"], hit["key"]))[:LIMIT]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Settings().sop_index_path)
    parser.add_argument("--query", help="Inspect ranking for a development question.")
    args = parser.parse_args()
    if args.query:
        index = load(args.output)
        for hit in search(index, args.query):
            print(hit["key"], hit["heading"], hit["score"], ",".join(hit["matched_terms"]))
    else:
        index = build(SOP_DIR, args.output)
        print(f"Indexed {len(index['sections'])} sections, {index['method']}, corpus {index['corpus_version'][:12]}")


if __name__ == "__main__":
    main()
