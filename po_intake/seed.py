from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from po_intake.config import Settings
from po_intake.db import Customer, CustomerAlias, Price, SKU, SKUAlias, make_engine


def seed(engine):
    with Session(engine) as db, db.begin():
        for model, rows in (
            (Customer, [
                {"reference": "CUST-101", "name": "Kencana Engineering"},
                {"reference": "CUST-102", "name": "Selangor Fabrication"},
            ]),
            (CustomerAlias, [{"alias": "KENCANA", "customer": "CUST-101"}]),
            (SKU, [
                {"code": "BRG-6204", "description": "Deep groove bearing"},
                {"code": "BELT-A42", "description": "Industrial V-belt"},
            ]),
            (SKUAlias, [{"alias": "6204-BRG", "sku": "BRG-6204"}]),
            (Price, [
                {"customer": "CUST-101", "sku": "BRG-6204", "currency": "MYR", "amount": Decimal("100.00")},
                {"customer": "CUST-101", "sku": "BELT-A42", "currency": "MYR", "amount": Decimal("45.50")},
                {"customer": "CUST-102", "sku": "BRG-6204", "currency": "MYR", "amount": Decimal("100.00")},
                {"customer": "CUST-102", "sku": "BELT-A42", "currency": "MYR", "amount": Decimal("45.50")},
            ]),
        ):
            db.execute(insert(model).values(rows).on_conflict_do_nothing())


if __name__ == "__main__":
    seed(make_engine(Settings().database_url))
    print("Demo catalog and agreed prices seeded.")
