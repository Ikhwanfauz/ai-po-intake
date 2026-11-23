import os

import httpx
import streamlit as st


API = os.getenv("PO_API_URL", "http://127.0.0.1:8000")
TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "180")) + 30

st.set_page_config(page_title="Meridian PO Intake", layout="wide")


def request(path, method="GET", **kwargs):
    try:
        response = httpx.request(method, f"{API.rstrip('/')}{path}", timeout=TIMEOUT, trust_env=False, **kwargs)
        if response.is_error:
            detail = response.json().get("detail", response.reason_phrase)
            if isinstance(detail, dict):
                st.error(detail["message"])
                for issue in detail.get("issues", []):
                    st.warning(issue)
            else:
                st.error(str(detail))
            return None
        return response
    except httpx.RequestError:
        st.error("The local API is unavailable or the request timed out. Refresh to check the saved status.")
    except ValueError:
        st.error("The local API returned an unexpected response.")
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
    st.caption(f"Approved by {order['reviewer']} · Snapshot {order['snapshot_id']}")
    st.dataframe(order["lines"], hide_index=True, width="stretch")


def review(intake):
    path = f"/intakes/{intake['id']}"
    st.subheader("Extraction and review")
    st.write(f"Intake **#{intake['id']}** · **{intake['state'].title()}**")
    attempt = intake["attempt"]
    if attempt:
        st.caption(f"Attempt #{attempt['id']} · {attempt['started_at']} · {attempt['settings']['model']}")
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

    draft = attempt["draft"]
    validation = attempt["validation"]
    st.write(
        f"PO **{draft['po_number']['value'] or '—'}** · "
        f"Customer **{draft['customer_reference']['value'] or '—'}** · "
        f"Currency **{draft['currency']['value'] or '—'}**"
    )
    rows = [
        {
            "SKU": line["sku"]["value"], "Quantity": line["quantity"]["value"],
            "Unit": line["unit"], "Unit price": line["unit_price"]["value"],
            "Agreed MYR price": checked["expected_price"],
        }
        for line, checked in zip(draft["lines"], validation["lines"])
    ]
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")
    for issue in validation["issues"]:
        st.warning(issue)
    with st.expander("Model output"):
        st.caption("Check proposed page references and excerpts against the original PDF.")
        st.json(draft)
    if intake["order"]:
        st.success(f"Order created · {intake['order']['number']}")
        show_order(intake["order"]["id"])
    else:
        checked = st.checkbox("I checked the customer, PO number, and all lines against the PDF.",
                              key=f"checked-{attempt['id']}")
        if st.button("Approve snapshot", type="primary", disabled=bool(validation["issues"]) or not checked):
            if call(f"{path}/approve", "POST", json={"snapshot_id": attempt["id"]}) is not None:
                st.rerun()
        if validation["issues"] and st.button("Recheck agreed prices"):
            if call(f"{path}/validate", "POST", json={"snapshot_id": attempt["id"]}) is not None:
                st.rerun()


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
                    process(receipt["id"])
        intakes = call("/intakes")
        if intakes:
            labels = {row["id"]: f"#{row['id']} · {row['filename']} · {row['state'].title()}" for row in intakes}
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
