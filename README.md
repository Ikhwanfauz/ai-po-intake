# Meridian PO Intake

Sales operations teams receive purchase orders as PDFs and must check what the customer actually ordered, whether the prices match the agreement, and which revision can be booked. A resend or a corrected attachment can lead to a duplicate order or an approval against an outdated price. Meridian turns each PDF into a reviewable draft with source evidence and records which revision a staff member approved.

![PO-104 V1 PDF beside the review panel showing the RM90.00 unit price, the agreed RM100.00, and the Superseded status](docs/screenshots/po-104-price-mismatch.png)

*PO-104 V1 shows RM90.00 against the agreed RM100.00. V1 is marked Superseded after V2 activation.*

## Walkthrough · PO-104 from PDF to sales order

1. **Catch the price mismatch.** Upload V1 and V2 in that order, then open the V1 row in the newest-first **Intake receipts** list. The PDF really says **RM90.00** for bearing `BRG-6204`. The agreed price is **RM100.00**, so approval is blocked even though the extraction is correct. [PO-104 V1 PDF](output/pdf/PO-104-V1.pdf)
2. **Review and approve the correction.** Open the V2 row from the same list. The comparison shows the unit price changing to **RM100.00** and the quantity staying at two. Review the source, enter a reason, click **Activate revision**, then complete the review and click **Approve snapshot**. V1 becomes **Superseded** and the resulting sales order references V2. [PO-104 V2 PDF](output/pdf/PO-104-V2.pdf)

   ![Intake receipts list with the original PO-104 V2 receipt selected and a newer V2 row marked Exact resend](docs/screenshots/intake-receipts.png)

   *Staff can open the original V2 receipt and identify its Exact resend in the list.*

3. **Read the approved order in chat.** In **PO review chat**, ask “Which unit price and revision are active on this PO?” The chatbot reads the active snapshot and linked sales order through a local, read-only MCP tool. The answer cites PO-104 V2, shows the RM100.00 price, and reports the sales order number. Expand **MCP tool trace** to see the selected PO, snapshot ID, checked and agreed prices, and approval status.

   ![PO review chat showing approved PO-104 V2, sales order SO-000001, and the expanded get_po_review_context tool trace](docs/screenshots/po-review-chat-mcp.png)

   *PO-104 V2 is approved as `SO-000001`. The expanded MCP trace shows the `get_po_review_context` call.*

The list shows filename, source, received time, and status. Upload the V2 copy to see **Exact resend** in the list. Open that row to find the link to the original. The same sales order remains on record. [V2 copy PDF](output/pdf/PO-104-V2-copy.pdf)

## Architecture

```mermaid
flowchart LR
    UI["Streamlit<br/>upload, review, chat"] <-->|HTTP| API["FastAPI"]
    Inbox["Gmail inbox"] --> N8N["n8n"]
    N8N <-->|"Intake and notification state"| API
    N8N --> Delivery["Telegram alerts<br/>Gmail confirmations"]
    API <-->|"Receipts, revisions, orders"| DB["PostgreSQL + local PDF storage"]
    DB <-->|"Jobs and source PDFs"| Worker["Extraction worker"]
    Worker --> Models["Ollama + optional LangGraph review"]
    API -->|"stdio"| MCP["Read-only MCP tool"]
    MCP -->|"Read selected PO"| API
    API --> SOP["Versioned SOP index"]
```

Ollama proposes structured fields and source excerpts. Backend checks tie each value to its PDF page and row, then compare the extracted prices with the seeded customer catalog. Staff can correct a reading with evidence and a reason. Approval rechecks prices and pending revisions against the active snapshot before creating one order per customer PO.

PostgreSQL stores extraction job leases and notification state. n8n submits PDFs to FastAPI, sends alerts and confirmations through the providers, and reports the outcomes back to FastAPI. Chat reads the selected PO through MCP and shows indexed SOP guidance when a question calls for it.

## Demo guides and verification

- **Local upload demo.** The setup below replays known extraction drafts for the shipped PO-104 and PO-105 fixtures and runs the normal evidence, revision, approval, and MCP paths. Ollama runs live for chat and for PDFs outside the replay set, including [PO-103](output/pdf/PO-103.pdf). Outbound delivery is disabled.
- **Separate live Gmail and Telegram flow.** A test-account run recorded both PO-104 PDFs arriving through Gmail. The database shows one Telegram alert for V1, one sales order approved from V2, and one Gmail confirmation for V2. Both sends have provider acknowledgements. The setup guide includes the earlier credential-free check, OAuth setup, and the two-message walkthrough. [Windows guide for Gmail and Telegram](docs/milestone-3.md)
- **LangGraph review.** Follow a targeted reread and inspect how an ambiguous PDF is held for staff review. [Windows guide for LangGraph](docs/milestone-4.md)
- **MCP and SOP chat.** Seed an open PO-106 V2 and run the grounding evaluation. Open the fixture from **Intake receipts**. [Windows guide for MCP and SOP chat](docs/milestone-5.md)

## LangGraph benchmark

The paired benchmark ran baseline extraction and forced LangGraph review on the same 12 held-out synthetic PDFs using `qwen3:8b`. **Accuracy was unchanged and median extraction-through-validation latency increased by 4.14 s.** The graph saved review traces and attempted targeted rereads. The backend rejected all four proposed patches because they were out of scope or unsupported by the source. [Per-PDF benchmark results](docs/evidence/milestone4/paired-results.md)

| Measure | Baseline | LangGraph |
| --- | ---: | ---: |
| Correct populated fields | 85/86 | 85/86 |
| Correct fields with verified evidence | 80/86 | 80/86 |
| Correctly routed PDFs | 10/12 | 10/12 |
| Correctly handled expected blanks or ambiguities | 0/4 | 0/4 |
| Median extraction-through-validation time | 7.87 s | 12.01 s |

## Run the local demo on Windows

Start Docker Desktop and Ollama. Run the commands from the repository root in PowerShell. If needed, create `.venv` with `python -m venv .venv` using Python 3.12 and download the model with `ollama pull qwen3:8b`.

**PowerShell 1 — API, database, and SOP index**

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
docker compose up -d --wait db

$demoDb = 'po_demo_intake_' + (Get-Date -Format yyyyMMdd_HHmmss)
docker compose exec -T db createdb -U po_demo $demoDb
$env:DATABASE_URL = "postgresql+psycopg://po_demo:po_demo@127.0.0.1:55432/$demoDb"
$env:PDF_STORAGE = "data/$demoDb/pdf"
$env:PO_SOP_INDEX = "data/$demoDb/sop-index.json"
$env:PO_MCP_API_URL = 'http://127.0.0.1:8006'
$env:PO_FOREGROUND_EXTRACTION = 'true'
$env:PO_GRAPH_AUTO = 'false'
$env:PO_OUTBOUND_ENABLED = 'false'
$env:PO_STAFF_KEY = ''
$env:OLLAMA_URL = 'http://127.0.0.1:11434'
$env:OLLAMA_MODEL = 'qwen3:8b'

.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m po_intake.seed
.\.venv\Scripts\python.exe -m po_intake.sop_index
.\.venv\Scripts\python.exe -m uvicorn scripts.milestone2_demo:app --host 127.0.0.1 --port 8006
```

**PowerShell 2 — dashboard**

```powershell
$env:PO_API_URL = 'http://127.0.0.1:8006'
$env:PO_FOREGROUND_EXTRACTION = 'true'
$env:PO_STAFF_KEY = ''
.\.venv\Scripts\python.exe -m streamlit run dashboard.py --server.address 127.0.0.1 --server.port 8506 --server.headless true
```

Open the [dashboard](http://127.0.0.1:8506) and choose **Upload and extract** for V1, then V2. Use each row's **Open** button to switch receipts. The [API docs](http://127.0.0.1:8006/docs) are available while the API is running.

### Useful local checks

For an existing demo database, use its `DATABASE_URL` and `PDF_STORAGE` with `alembic upgrade head` and `po_intake.seed`. Existing orders retain their order and snapshot IDs. Open migrated drafts can be assessed with **Recheck draft**.

The [fixture folder](output/pdf/) contains the shipped PDFs. Regenerate the base set with `.\.venv\Scripts\python.exe scripts/generate_fixture.py`. For a retry walkthrough, generate a distinct PDF so it enters intake as a new document.

```powershell
.\.venv\Scripts\python.exe -c "from pathlib import Path; from scripts.generate_fixture import generate; generate(Path('tmp/PO-106-retry.pdf'), 'PO-106')"
```

Stop the API with Ctrl+C, set `$env:OLLAMA_URL = 'http://127.0.0.1:1'`, restart it with the command above, and upload the generated PDF. Restore `http://127.0.0.1:11434` and restart the API. Refresh the dashboard and select **Retry extraction** on the failed receipt to reuse the stored PDF.

Run the checks with the Compose database running and the migrated demo's `DATABASE_URL` set. Pytest creates temporary schemas and PDF storage.

```powershell
docker compose config --quiet
.\.venv\Scripts\python.exe -m alembic check
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q
```
