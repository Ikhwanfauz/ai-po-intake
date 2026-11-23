import re
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from po_intake.db import Customer, CustomerAlias, Price, SKU, SKUAlias


def identity(db, proposal, model, alias_model, reference_attr):
    if not proposal.value or proposal.alternatives:
        return None
    value = proposal.value.strip()
    if db.get(model, value):
        return value
    alias = db.get(alias_model, value)
    return getattr(alias, reference_attr) if alias else None


def validate(db, draft, pages, customer_context, lock_prices=False, check_prices=True):
    issues = [f"Page {page['page']} has no readable text." for page in pages if not page["text"].strip()]
    customer = identity(db, draft.customer_reference, Customer, CustomerAlias, "customer")
    if customer is None:
        issues.append("Customer reference is missing, ambiguous, or unknown.")
    elif customer_context and customer_context != customer:
        issues.append("The extracted customer conflicts with the selected customer.")
    po_number = (draft.po_number.value or "").strip()
    if not po_number or draft.po_number.alternatives:
        issues.append("PO number is missing or ambiguous.")
    currency = (draft.currency.value or "").strip().upper()
    if currency != "MYR" or draft.currency.alternatives:
        issues.append("The order must explicitly use MYR (RM).")
    if not draft.lines:
        issues.append("The draft has no order lines.")

    lines = []
    price_issues = []
    for position, line in enumerate(draft.lines, start=1):
        prefix = f"Line {position}"
        sku = identity(db, line.sku, SKU, SKUAlias, "sku")
        if sku is None:
            issues.append(f"{prefix} SKU is missing, ambiguous, or unknown.")

        quantity = (line.quantity.value or "").strip()
        if line.quantity.alternatives or not re.fullmatch(r"[0-9]{1,10}", quantity):
            quantity = None
        else:
            quantity = int(quantity)
        if quantity is None or not 1 <= quantity <= 2_147_483_647:
            issues.append(f"{prefix} requires a positive whole-unit quantity.")

        if line.unit and line.unit.strip().upper() not in {"EA", "EACH", "PC", "PCS", "UNIT", "UNITS"}:
            issues.append(f"{prefix} has an unsupported unit {line.unit!r}.")

        token = (line.unit_price.value or "").strip()
        actual = None
        if not line.unit_price.alternatives and re.fullmatch(r"(?:RM\s*)?[0-9]{1,10}(?:\.[0-9]{1,2})?", token, re.I):
            actual = Decimal(re.sub(r"^RM\s*", "", token, flags=re.I))
        if actual is None:
            issues.append(f"{prefix} requires an unambiguous, nonnegative unit price with at most two decimals.")

        expected = None
        if check_prices and customer and customer_context in {None, customer} and sku and currency == "MYR":
            statement = select(Price.amount).where(
                Price.customer == customer, Price.sku == sku, Price.currency == currency
            )
            if lock_prices:
                statement = statement.with_for_update(read=True)
            expected = db.scalar(statement)
            if expected is None:
                price_issues.append(f"{prefix} has no agreed MYR price.")
            elif actual is not None and actual != expected:
                price_issues.append(f"{prefix} unit price RM{actual:.2f} differs from agreed RM{expected:.2f}.")
        lines.append({
            "sku": sku, "quantity": quantity,
            "unit_price": f"{actual:.2f}" if actual is not None else None,
            "expected_price": f"{expected:.2f}" if expected is not None else None,
        })
    return {
        "customer": customer, "po_number": po_number, "currency": currency,
        "lines": lines, "issues": issues + price_issues, "field_issues": issues,
        "price_issues": price_issues, "checked_at": datetime.now(timezone.utc).isoformat(),
    }
