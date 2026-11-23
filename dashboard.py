import os

import httpx
import streamlit as st


API = os.getenv("PO_API_URL", "http://127.0.0.1:8000")
TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "180")) + 30

st.set_page_config(page_title="Meridian PO Intake", layout="wide")


def feedback(message, issues=()):
    st.error(message)
    for issue in issues:
        st.warning(issue)


def request(path, method="GET", capture=False, **kwargs):
    def report(message, issues=()):
        if capture:
            st.session_state["review_feedback"] = (message, issues)
        else:
            feedback(message, issues)

    try:
        response = httpx.request(method, f"{API.rstrip('/')}{path}", timeout=TIMEOUT, trust_env=False, **kwargs)
        if response.is_error:
            detail = response.json().get("detail", response.reason_phrase)
            if isinstance(detail, dict):
                report(detail["message"], detail.get("issues", []))
            else:
                report(str(detail))
            return None
        return response
    except httpx.RequestError:
        report("The local API is unavailable or the request timed out. Refresh to check the saved status.")
    except ValueError:
        report("The local API returned an unexpected response.")
    return None


def call(path, method="GET", **kwargs):
    response = request(path, method, **kwargs)
    return response.json() if response is not None else None


def process(intake_id):
    with st.spinner("Extracting from the stored PDF with Ollama…"):
        return call(f"/intakes/{intake_id}/process", "POST")


def original_pdf(intake):
    path = f"/intakes/{intake['id']}"
    st.subheader("Original purchase order")
    first = request(f"{path}/pages/1")
    if first is None:
        return
    count = int(first.headers["X-Page-Count"])
    page = st.selectbox("PDF page", range(1, count + 1), key=f"page-{intake['id']}")
    preview = first if page == 1 else request(f"{path}/pages/{page}")
    if preview is not None:
        st.image(preview.content, width="stretch")
    st.link_button("Download original PDF", f"{API}{path}/pdf")


def show_order(order_id):
    order = call(f"/orders/{order_id}")
    if order is None:
        return
    st.subheader(order["number"])
    st.write(f"Customer PO **{order['po_number']}** · {order['customer']} · {order['currency']}")
    st.caption(f"Approved by {order['reviewer']} · V{order['version']} · Snapshot {order['snapshot_id']}")
    st.dataframe(order["lines"], hide_index=True, width="stretch")


def submit_review(path, payload, widget_fields=None):
    # Callbacks submit the IDs shown before the rerun fetches more recent review data.
    if widget_fields:
        payload = {**payload, **{
            field: st.session_state[key] for field, key in widget_fields.items()
        }}
    request(path, "POST", json=payload, capture=True)


def link_to_receipt(label, intake_id):
    st.link_button(label, f"?intake={intake_id}")


def field_rows(assessment):
    def row(label, entry, expected=None):
        return {"Field": label, "Value": entry["value"], "Agreed MYR": expected, "Page": entry["page"],
                "Verified excerpt": entry.get("excerpt"),
                "Evidence": entry["issue"] if entry["status"] != "verified" else "Verified"}

    evidence = assessment["evidence"]
    rows = [row(label, evidence["fields"][name]) for name, label in (
        ("po_number", "PO number"), ("customer_reference", "Customer reference"), ("currency", "Currency"),
    )]
    for index, (line, checked) in enumerate(zip(evidence["lines"], assessment["validation"]["lines"]), 1):
        for name, entry in line.items():
            expected = checked["expected_price"] if name == "unit_price" else None
            rows.append(row(f"Line {index} · {name.replace('_', ' ')}", entry, expected))
    return rows


DISPOSITION = {
    "unlinked": "Identity review",
    "active": "Active",
    "pending": "Pending revision",
    "equivalent": "Equivalent copy",
    "superseded": "Superseded",
    "dismissed": "Dismissed",
    "post_order": "Post-order follow-up",
}


def document_name(entry):
    return f"V{entry['number']}" if entry["number"] else "Unlinked document"


def show_history(intake):
    with st.expander("Version, receipt, and edit history"):
        for entry in intake["history"]:
            st.write(f"**{document_name(entry)} · {DISPOSITION[entry['disposition']]}**")
            link_to_receipt(f"Open {entry['filename']}", entry["intake_id"])
            for receipt in entry["receipts"]:
                kind = "Exact resend" if receipt["canonical_intake_id"] else "Original delivery"
                st.caption(f"Intake #{receipt['id']} · {kind} · {receipt['filename']} · {receipt['received_at']}")
            for edit in entry["edits"]:
                st.caption(f"Edit {edit['edit']} · Snapshot {edit['id']} · {edit['author']} · {edit['created_at']}")
                st.text(edit["reason"])
        if intake["receipt_edit_history"]:
            st.write("Review before this receipt was linked")
            for edit in intake["receipt_edit_history"]:
                st.caption(f"Edit {edit['edit']} · Snapshot {edit['id']} · {edit['author']}")
                st.text(edit["reason"])
        for event in intake["activity"]:
            if event["action"] != "validation":
                st.caption(f"{event['action'].replace('_', ' ').capitalize()} · {event['author']} · {event['created_at']}")
                st.json(event["detail"], expanded=False)


def compare(intake):
    comparison = intake["comparison"]
    if not comparison:
        return
    st.subheader(f"Compare with active V{intake['po']['active_version']}")
    link_to_receipt("Open active document", comparison["active_intake_id"])
    st.text(f"Currency  {comparison['active_currency']} → {comparison['candidate_currency']}")
    rows = []
    for line in comparison["lines"]:
        old, new = line["active"] or {}, line["candidate"] or {}
        rows.append({"SKU": line["sku"], "Active qty": old.get("quantity"), "Selected qty": new.get("quantity"),
                     "Active unit price": old.get("unit_price"), "Selected unit price": new.get("unit_price"),
                     "Comparison": line["status"].capitalize()})
    st.dataframe(rows, hide_index=True, width="stretch")
    if any(row["Comparison"] == "Unmatched" for row in rows):
        st.warning("Unmatched rows need manual comparison before activation.")


def correct_draft(path, snapshot, attempt, payload, allow_context):
    with st.expander("Correct a reading"):
        draft = snapshot["draft"]
        choices = [
            ("PO number", "po_number", None), ("Customer reference", "customer_reference", None),
            ("Currency", "currency", None),
        ]
        for number in range(1, len(draft["lines"]) + 1):
            choices.extend((f"Line {number} · {label}", field, number) for label, field in (
                ("SKU", "sku"), ("Quantity", "quantity"), ("Unit price", "unit_price"),
                ("Unit", "unit"), ("Remove this line", "remove_line"),
            ))
        choices.append(("Add a line · SKU", "sku", len(draft["lines"]) + 1))
        _, field, line = st.selectbox("Field to correct", choices, format_func=lambda item: item[0],
                                     key=f"field-{snapshot['id']}")
        source = draft if line is None else draft["lines"][line - 1] if line <= len(draft["lines"]) else {}
        current = source.get(field) or {}
        if isinstance(current, str):
            current = {"value": current}
        key = f"edit-{snapshot['id']}-{field}-{line}"
        with st.form(key):
            keys = {name: f"{key}-{name}" for name in ("value", "page", "excerpt", "reason")}
            st.text_input("Value as printed", value=current.get("value") or "", key=keys["value"],
                          disabled=field == "remove_line")
            st.number_input("Source page", min_value=1, value=current.get("page") or 1, key=keys["page"])
            st.text_area("Exact source excerpt", value=current.get("excerpt") or "", key=keys["excerpt"])
            st.text_input("Reason for correction", key=keys["reason"])
            if field == "customer_reference" and allow_context:
                keys["confirm_customer"] = f"{key}-confirm"
                st.checkbox("Confirm this source-supported customer for the intake", key=keys["confirm_customer"])
            st.form_submit_button("Save correction", on_click=submit_review,
                                  args=(f"{path}/correct", {**payload, "field": field, "line": line}, keys))
        with st.expander("Extracted PDF page text"):
            for page in attempt["pages"] or []:
                st.caption(f"Page {page['page']}")
                st.code(page["text"], language=None)


def review(intake):
    path = f"/intakes/{intake['id']}"
    st.subheader("Extraction and review")
    st.write(f"Intake **#{intake['id']}** · **{intake['state'].title()}**")
    attempt = intake["attempt"]
    canonical = intake["canonical_intake_id"]
    if canonical:
        st.info(f"Exact resend · Original intake #{canonical}")
        link_to_receipt("Open original receipt", canonical)
    if attempt:
        source = attempt["settings"].get("source") or attempt["settings"].get("model", "Ollama")
        st.caption(f"Attempt #{attempt['id']} · {attempt['started_at']} · {source}")
    if intake["context_issue"] and not intake["snapshot"]:
        st.warning(intake["context_issue"])
    if intake["state"] in {"pending", "failed"}:
        if attempt and attempt["error"]:
            st.error(attempt["error"])
        label = "Retry extraction" if intake["state"] == "failed" else "Process stored PDF"
        if st.button(label, type="primary") and process(intake["id"]) is not None:
            st.rerun()
        return
    if intake["state"] == "processing":
        stopped = st.checkbox("I confirmed the previous API process stopped.", key=f"stopped-{attempt['id']}")
        if st.button("Mark attempt interrupted", disabled=not stopped):
            if call(f"{path}/interrupt", "POST", json={"attempt_id": attempt["id"]}) is not None:
                st.rerun()
        return

    po, version, snapshot = intake["po"], intake["version"], intake["snapshot"]
    assessment = snapshot["assessment"]
    disposition = version["disposition"]
    active = po["active_snapshot_id"] if po else None
    payload = {"snapshot_id": snapshot["id"], "active_snapshot_id": active}
    st.subheader(f"{document_name(version)} · {DISPOSITION[disposition]}")
    st.caption(f"Edit {snapshot['edit']} · Snapshot {snapshot['id']}")
    if assessment:
        st.dataframe(field_rows(assessment), hide_index=True, width="stretch")
        for issue in assessment["issues"]:
            st.warning(issue)
    elif intake["order"]:
        st.info("Approved during Milestone 1. The original approved snapshot is available below.")
    else:
        st.info("Check the migrated extraction before approving it.")
    if intake["identity_options"]:
        options = intake["identity_options"]
        if options["customers"] or options["po_numbers"]:
            st.text("Customer candidates: " + ", ".join(options["customers"]))
            st.text("PO number candidates: " + ", ".join(options["po_numbers"]))
        if options["orders"]:
            st.text("Matching records to review: " + ", ".join(item["po_key"] for item in options["orders"]))
    if po and po["pending_versions"]:
        st.warning("Pending revisions block approval: " + ", ".join(f"V{n}" for n in po["pending_versions"]))
    compare(intake)
    with st.expander("Original model output"):
        if attempt["raw"]:
            st.code(attempt["raw"], language="json", wrap_lines=True)
        else:
            st.json(attempt["draft"])
    show_history(intake)
    if intake["order"]:
        st.success(f"Confirmed order · {intake['order']['number']}")
        show_order(intake["order"]["id"])
        if disposition != "post_order":
            return
    if canonical:
        return
    if disposition in {"unlinked", "active", "pending", "post_order"}:
        correct_draft(path, snapshot, attempt, payload, po is None)
        if assessment is None or assessment["issues"]:
            st.button("Recheck draft", on_click=submit_review,
                      args=(f"{path}/validate", {"snapshot_id": snapshot["id"]}))
    if disposition == "active":
        checked = st.checkbox("I checked the customer, PO number, and all lines against the PDF.",
                              key=f"checked-{snapshot['id']}")
        st.button("Approve snapshot", type="primary",
                  disabled=not checked or not assessment or bool(assessment["issues"] or po["pending_versions"]),
                  on_click=submit_review, args=(f"{path}/approve", {"snapshot_id": snapshot["id"]}))
    if disposition == "pending":
        reason = f"revision-reason-{snapshot['id']}"
        with st.form(f"revision-{snapshot['id']}"):
            st.text_input("Reason for revision decision", key=reason)
            st.form_submit_button("Activate revision", type="primary", on_click=submit_review,
                                  args=(f"{path}/activate", payload, {"reason": reason}))
            st.form_submit_button("Dismiss revision", on_click=submit_review,
                                  args=(f"{path}/dismiss", payload, {"reason": reason}))


def main():
    st.title("Meridian PO Intake")
    customer_list = call("/customers")
    if customer_list is None:
        return
    customers = {row["reference"]: row["name"] for row in customer_list}
    intake_tab, orders_tab = st.tabs(["Intake", "Sales orders"])

    with intake_tab:
        with st.expander("Upload a purchase order", expanded=True):
            with st.form("upload"):
                file = st.file_uploader("Purchase order PDF", type="pdf")
                context = st.selectbox("Customer context (optional)", [None, *customers],
                                       format_func=lambda key: customers[key] if key else "Use the PDF reference")
                submit = st.form_submit_button("Upload and extract", type="primary")
            if submit and file is None:
                st.error("Choose a purchase order PDF to upload.")
            if submit and file is not None:
                receipt = call("/intakes", "POST", files={"file": (file.name, file.getvalue(), "application/pdf")},
                               data={"customer_context": context or ""})
                if receipt is not None:
                    st.session_state["intake_select"] = receipt["id"]
                    st.query_params["intake"] = receipt["id"]
                    if receipt["state"] == "pending":
                        process(receipt["id"])
        intakes = call("/intakes")
        if intakes:
            labels = {
                row["id"]: f"#{row['id']} · {row['filename']} · "
                f"{'Exact resend' if row['canonical_intake_id'] else row['state'].title()}"
                for row in intakes
            }
            selected = st.query_params.get("intake", "")
            default = next((i for i, key in enumerate(labels) if str(key) == selected), 0)
            if "intake_select" not in st.session_state:
                st.session_state["intake_select"] = list(labels)[default]
            left, right = st.columns([4, 1], vertical_alignment="bottom")
            with left:
                intake_id = st.selectbox("Intake receipt", list(labels), format_func=labels.get, key="intake_select")
                if str(intake_id) != selected:
                    st.query_params["intake"] = intake_id
            with right:
                st.button("Refresh")
            message = st.session_state.pop("review_feedback", None)
            if message:
                feedback(*message)
            intake = call(f"/intakes/{intake_id}")
            if intake is not None:
                pdf_col, draft_col = st.columns([1, 1])
                with pdf_col:
                    original_pdf(intake)
                with draft_col:
                    review(intake)
        elif intakes == []:
            st.info("Upload a purchase order PDF to start a review.")

    with orders_tab:
        orders = call("/orders")
        if orders:
            labels = {order["id"]: f"{order['number']} · {order['po_number']}" for order in orders}
            order_id = st.selectbox("Stored sales order", list(labels), format_func=labels.get)
            show_order(order_id)
        elif orders == []:
            st.info("Approved purchase orders appear here.")


main()
