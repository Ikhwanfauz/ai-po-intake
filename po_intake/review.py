from collections import Counter

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from po_intake.db import Attempt, Document, ExtractionJob, Intake, PurchaseOrder, ReviewEvent, Snapshot, Version
from po_intake.evidence import HEADERS, source_pages, verify
from po_intake.extraction import Draft
from po_intake.validation import validate


CONTEXT_CONFLICT = "This delivery's customer context differs from an earlier receipt. Verify the PDF customer before linking."


def version_for(db, intake_id):
    return db.scalar(select(Version).where(Version.intake_id == intake_id))


def current_snapshot(db, version):
    return db.scalar(
        select(Snapshot).where(Snapshot.version_id == version.id).order_by(Snapshot.edit.desc()).limit(1)
    )


def next_edit_id(db):
    # Baseline snapshots retain their attempt IDs. Edits share the sequence to keep those IDs distinct.
    return db.scalar(text("SELECT nextval(pg_get_serial_sequence('attempts', 'id'))"))


def assess(db, draft, pages, context, context_issue=None, lock_prices=False, *, graph_issues=()):
    evidence = verify(draft, pages)
    checked = draft.model_copy(deep=True)
    for line, findings in zip(checked.lines, evidence["lines"]):
        if "unit" in findings:
            line.unit = findings["unit"]["value"]
    check_prices = not context_issue and evidence["fields"]["customer_reference"]["status"] == "verified"
    validation = validate(db, checked, pages or [], context, lock_prices=lock_prices, check_prices=check_prices)
    evidence_issues = evidence["issues"]
    issues = validation["issues"] + evidence_issues + list(graph_issues)
    if context_issue:
        issues.append(context_issue)
    return {
        "context": context, "context_issue": context_issue,
        "evidence": evidence, "validation": validation, "issues": issues,
        **({"graph_issues": list(graph_issues)} if graph_issues else {}),
    }


def latest_assessment(db, snapshot):
    refreshed = db.scalar(
        select(ReviewEvent.detail).where(
            ReviewEvent.snapshot_id == snapshot.id, ReviewEvent.action == "validation",
        ).order_by(ReviewEvent.id.desc()).limit(1)
    )
    return refreshed if refreshed is not None else snapshot.assessment


def record(db, version, snapshot, action, author, detail):
    db.add(ReviewEvent(version_id=version.id, snapshot_id=snapshot.id if snapshot else None,
                       action=action, author=author, detail=detail))


def snapshot_view(db, snapshot):
    return {
        "id": snapshot.id, "edit": snapshot.edit, "parent_id": snapshot.parent_id,
        "author": snapshot.author, "reason": snapshot.reason, "created_at": snapshot.created_at,
        "draft": snapshot.draft, "assessment": latest_assessment(db, snapshot),
    }


def identity_key(assessment):
    validation = assessment["validation"]
    fields = assessment["evidence"]["fields"]
    customer, po_number = validation["customer"], validation["po_number"]
    if (customer and po_number and not assessment["context_issue"]
            and assessment["context"] in {None, customer}
            and all(fields[name]["status"] == "verified" for name in ("po_number", "customer_reference"))):
        return customer, po_number.casefold()
    return None


def complete_content(assessment):
    if assessment is None or identity_key(assessment) is None:
        return None
    validation = assessment["validation"]
    if validation["field_issues"] or assessment["evidence"]["issues"] or assessment.get("graph_issues"):
        return None
    counter = Counter((line["sku"], line["quantity"], line["unit_price"]) for line in validation["lines"])
    return validation["currency"], counter


def equivalent(db, po, assessment):
    active = db.get(Snapshot, po.active_snapshot_id)
    left = complete_content(latest_assessment(db, active))
    if left is None and active.assessment is None:
        # An approved pre-migration draft can be compared if its source also verifies.
        intake = db.get(Intake, db.get(Version, active.version_id).intake_id)
        historical = assess(db, Draft.model_validate(active.draft), db.get(Attempt, active.attempt_id).pages,
                            intake.customer_context)
        left = complete_content(historical)
    return left is not None and left == complete_content(assessment)


def lock_po(db, po_id):
    return db.get(PurchaseOrder, po_id, with_for_update=True, populate_existing=True) if po_id else None


def blockers(db, po):
    return db.scalars(select(Version.number).where(
        Version.po_id == po.id, Version.disposition == "pending",
    ).order_by(Version.number)).all()


def reconcile_pending(db, po, author):
    for version in db.scalars(select(Version).where(
        Version.po_id == po.id, Version.disposition == "pending",
    )):
        snapshot = current_snapshot(db, version)
        if equivalent(db, po, latest_assessment(db, snapshot)):
            version.disposition = "equivalent"
            record(db, version, snapshot, "equivalent", author, {"version": version.number})


def update_status(db, po):
    if po.status != "ordered":
        assessment = latest_assessment(db, db.get(Snapshot, po.active_snapshot_id))
        ready = assessment is not None and not assessment["issues"] and not blockers(db, po)
        po.status = "ready_for_approval" if ready else "needs_review"


def exact_delivery(db, digest, context):
    # Upload holds the document row lock, so simultaneous compatible uploads share work.
    originals = db.scalars(select(Intake).where(
        Intake.digest == digest, Intake.canonical_id.is_(None),
    ).order_by(Intake.id)).all()
    provisional = None
    for prior in originals:
        version = version_for(db, prior.id)
        po = db.get(PurchaseOrder, version.po_id) if version and version.po_id else None
        if po:
            if context in {None, po.customer}:
                return prior, None
        elif prior.context_issue is None and context == prior.customer_context and provisional is None:
            provisional = prior
    if provisional:
        return provisional, None
    return None, CONTEXT_CONFLICT if originals else None


def link_version(db, intake, version, snapshot, assessment, author):
    key = identity_key(assessment)
    if key is None:
        return None
    customer, po_key = key
    db.get(Document, intake.digest, with_for_update=True)
    db.execute(insert(PurchaseOrder).values(customer=customer, po_key=po_key, status="needs_review")
               .on_conflict_do_nothing(constraint="unique_purchase_order_key"))
    po = db.scalar(select(PurchaseOrder).where(
        PurchaseOrder.customer == customer, PurchaseOrder.po_key == po_key,
    ).with_for_update())
    exact = db.scalar(select(Version).join(Intake).where(
        Version.po_id == po.id, Intake.digest == intake.digest,
    ).order_by(Version.number).limit(1))
    if exact:
        db.execute(update(Intake).where(Intake.canonical_id == intake.id).values(canonical_id=exact.intake_id))
        intake.canonical_id = exact.intake_id
        version.disposition = "exact_resend"
        record(db, version, snapshot, "exact_resend", author, {"canonical_intake_id": exact.intake_id})
        return po

    latest_number = db.scalar(select(func.max(Version.number)).where(Version.po_id == po.id))
    version.po_id = po.id
    version.number = (latest_number or 0) + 1
    if po.active_snapshot_id is None:
        version.disposition = "active"
        db.flush()
        po.active_version_id, po.active_snapshot_id = version.id, snapshot.id
    elif equivalent(db, po, assessment):
        version.disposition = "equivalent"
    else:
        version.disposition = "post_order" if po.status == "ordered" else "pending"
    record(db, version, snapshot, version.disposition, author, {"version": version.number})
    update_status(db, po)
    return po


def store_initial_review(db, intake, attempt, draft, *, graph_issues=()):
    version = Version(intake_id=intake.id, disposition="unlinked")
    db.add(version)
    db.flush()
    assessment = assess(db, draft, attempt.pages, intake.customer_context, intake.context_issue,
                        graph_issues=graph_issues)
    author = attempt.settings.get("source", "Ollama")
    snapshot = Snapshot(id=attempt.id, version_id=version.id, attempt_id=attempt.id, edit=1, author=author,
                        reason="Original extraction", draft=attempt.draft, assessment=assessment)
    db.add(snapshot)
    db.flush()
    if link_version(db, intake, version, snapshot, assessment, author) is None:
        record(db, version, snapshot, "unlinked", author, {"issues": assessment["issues"]})
    return assessment["validation"]


def identity_options(db, draft, pages, assessment):
    if assessment["context_issue"]:
        return {"customers": [], "po_numbers": [], "orders": []}
    customers, numbers = set(), set()
    for source in source_pages(pages):
        customers.update(cell.value for cell in source.headers["customer_reference"])
        numbers.update(cell.value for cell in source.headers["po_number"])
    for name, options in (("customer_reference", customers), ("po_number", numbers)):
        proposal = getattr(draft, name)
        options.update(proposal.alternatives)
        if proposal.value:
            options.add(proposal.value)
    customer = assessment["validation"]["customer"]
    orders = []
    if customer and assessment["context"] in {None, customer} and (
        assessment["evidence"]["fields"]["customer_reference"]["status"] == "verified"
    ):
        possible = {value.strip().casefold() for value in numbers}
        orders = [{"id": po.id, "po_key": po.po_key} for po in db.scalars(select(PurchaseOrder).where(
            PurchaseOrder.customer == customer, PurchaseOrder.po_key.in_(possible),
        ))]
    return {"customers": sorted(customers), "po_numbers": sorted(numbers), "orders": orders}


def isolate_conflicting_resends(db, intake, customer):
    # Keep the received context and reopen any linked receipt whose explicit context conflicts.
    detached = db.scalars(update(Intake).where(
        Intake.canonical_id == intake.id,
        Intake.customer_context.is_not(None), Intake.customer_context != customer,
    ).values(canonical_id=None, context_issue=CONTEXT_CONFLICT).returning(Intake.id)).all()
    if detached:
        db.execute(insert(ExtractionJob).values([{"intake_id": intake_id} for intake_id in detached])
                   .on_conflict_do_nothing())


def diff_lines(active, proposed):
    def lines(snapshot):
        assessment = snapshot.get("assessment")
        if assessment is not None:
            return assessment["validation"]["lines"]
        draft = Draft.model_validate(snapshot["draft"])
        return [{"sku": line.sku.value, "quantity": line.quantity.value,
                 "unit_price": line.unit_price.value} for line in draft.lines]

    old, new = lines(active), lines(proposed)
    left, right = Counter(line["sku"] for line in old), Counter(line["sku"] for line in new)
    matched = {sku for sku in left.keys() & right.keys() if sku and left[sku] == right[sku] == 1}
    rows = []
    for sku in sorted(matched):
        before = next(line for line in old if line["sku"] == sku)
        after = next(line for line in new if line["sku"] == sku)
        rows.append({"sku": sku, "active": before, "candidate": after,
                     "status": "changed" if any(before.get(key) != after.get(key)
                                                for key in ("quantity", "unit_price")) else "same"})
    for side, entries, own, other in (("active", old, left, right), ("candidate", new, right, left)):
        for line in entries:
            sku = line["sku"]
            if sku not in matched:
                status = "unmatched" if not sku or own[sku] > 1 or other[sku] > 1 else (
                    "removed" if side == "active" else "added"
                )
                rows.append({"sku": sku, "active": None, "candidate": None, **{side: line}, "status": status})
    return rows
