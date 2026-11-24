"""Seed two synthetic POs through the regular intake and revision routes."""
import argparse
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from po_intake.api import create_app
from po_intake.config import Settings
from po_intake.extraction import extract_with_ollama, pdf_pages
from scripts.generate_fixture import generate
from scripts.milestone2_demo import page_key, replay_fixture


FIXTURES = {
    "PO-106-V1.pdf": ("PO-106", 1, "RM90.00"),
    "PO-106-V2.pdf": ("PO-106", 2, "RM100.00"),
    "PO-107-V1.pdf": ("PO-107", 1, "RM95.00"),
}


def write_fixtures(directory):
    for filename, (po, version, price) in FIXTURES.items():
        generate(directory / filename, po, first_price=price, edition=f"Revision {version}")


@lru_cache(maxsize=1)
def fixture_pages():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        write_fixtures(root)
        return {page_key(pdf_pages((root / filename).read_bytes())) for filename in FIXTURES}


def demo_extractor(settings, pages):
    if page_key(pages) in fixture_pages():
        return replay_fixture(pages)
    return extract_with_ollama(settings, pages)


def demo_settings():
    return replace(Settings(), foreground=True, graph_auto=False, outbound_enabled=False)


app = create_app(demo_settings(), extractor=demo_extractor)


def upload_fixture(client, path):
    intake = client.post("/intakes", files={"file": (path.name, path.read_bytes(), "application/pdf")})
    receipt = intake.raise_for_status().json()
    return prepare_receipt(client, receipt)


def prepare_receipt(client, receipt):
    intake_id = receipt["canonical_intake_id"] or receipt["id"]
    if receipt["state"] in {"pending", "failed"}:
        return client.post(f"/intakes/{intake_id}/process").raise_for_status().json()
    return client.get(f"/intakes/{intake_id}").raise_for_status().json()


def activate_v2(client, detail):
    return client.post(f"/intakes/{detail['id']}/activate", json={
        "snapshot_id": detail["snapshot"]["id"], "active_snapshot_id": detail["po"]["active_snapshot_id"],
        "reason": "Synthetic customer correction to the agreed unit price.",
    }).raise_for_status().json()


def seed_fixtures(client, directory):
    write_fixtures(directory)
    existing = {}
    for row in client.get("/intakes").raise_for_status().json():
        existing.setdefault(row["filename"], row)
    seeded = {}
    for filename in FIXTURES:
        detail = (prepare_receipt(client, existing[filename]) if filename in existing
                  else upload_fixture(client, directory / filename))
        if detail["version"]["disposition"] == "pending" and filename == "PO-106-V2.pdf":
            detail = activate_v2(client, detail)
        seeded[filename] = detail
    return seeded


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=Path("data/milestone5/fixtures"))
    args = parser.parse_args()
    settings = Settings()
    headers = {"X-Staff-Key": settings.staff_key} if settings.staff_key else {}
    with httpx.Client(base_url=settings.mcp_api_url, headers=headers, timeout=30, trust_env=False) as client:
        seeded = seed_fixtures(client, args.fixtures)
    for filename in ("PO-106-V2.pdf", "PO-107-V1.pdf"):
        detail = seeded[filename]
        print(f"{filename}: intake {detail['id']}, PO {detail['po']['id']}, active V{detail['po']['active_version']}, "
              f"snapshot {detail['po']['active_snapshot_id']}, {detail['po']['status']}")


if __name__ == "__main__":
    main()
