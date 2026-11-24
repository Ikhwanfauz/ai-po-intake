# Milestone 4 on Windows

This walkthrough uses a separate PostgreSQL database and PDF directory. Start Docker Desktop and the Ollama Windows app. The API listens on port 8004 and the dashboard on 8504. Use a fresh PowerShell window for each process.

## Start the demo

Run the install and database creation once from the repository root. If the database already exists, reuse it or pick a fresh name and use it in each command below.

```powershell
# Run from your own repository root.
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
docker compose up -d --wait db
docker compose exec -T db createdb -U po_demo po_demo_m4
ollama list
```

If the tag is absent, run `ollama pull qwen3:8b`. The database default below leaves live delivery disabled.

**API window**

```powershell
# Run from your own repository root.
$env:DATABASE_URL = 'postgresql+psycopg://po_demo:po_demo@127.0.0.1:55432/po_demo_m4'
$env:PDF_STORAGE = 'data/po_demo_m4/pdf'
$env:OLLAMA_URL = 'http://127.0.0.1:11434'
$env:OLLAMA_MODEL = 'qwen3:8b'
$env:PO_OUTBOUND_ENABLED = 'false'
$env:PO_FOREGROUND_EXTRACTION = 'false'
$env:PO_STAFF_KEY = ''
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m po_intake.seed
.\.venv\Scripts\python.exe -m uvicorn po_intake.api:app --host 127.0.0.1 --port 8004
```

**Worker window**

```powershell
# Run from your own repository root.
$env:DATABASE_URL = 'postgresql+psycopg://po_demo:po_demo@127.0.0.1:55432/po_demo_m4'
$env:PDF_STORAGE = 'data/po_demo_m4/pdf'
$env:OLLAMA_URL = 'http://127.0.0.1:11434'
$env:OLLAMA_MODEL = 'qwen3:8b'
$env:PO_OUTBOUND_ENABLED = 'false'
$env:PO_GRAPH_AUTO = 'true'
.\.venv\Scripts\python.exe -m po_intake.worker --graph-demo
```

The walkthrough flag recognizes the shipped PO-201 page text and withholds the printed page 2 unit price from the first real Ollama proposal. It saves the original model response. The Review Agent, scoped reread, and second review use real Ollama calls. The ambiguity fixture and other PDFs use the normal extractor. Omit the flag for normal operation.

**Dashboard window**

```powershell
# Run from your own repository root.
$env:PO_API_URL = 'http://127.0.0.1:8004'
$env:PO_FOREGROUND_EXTRACTION = 'false'
$env:PO_STAFF_KEY = ''
.\.venv\Scripts\python.exe -m streamlit run dashboard.py --server.address 127.0.0.1 --server.port 8504 --server.headless true
```

## Walk through the two fixtures

Open [the dashboard](http://127.0.0.1:8504).

1. Upload [PO-201-multipage.pdf](../output/pdf/milestone4/demo/PO-201-multipage.pdf). Click **Refresh** after the worker finishes. Open **LangGraph review** and select the automatic run. Follow the initial proposal, the review request for page 2 and `lines.2.unit_price`, the targeted reread, and the second review. Compare the resulting RM45.50 and its verified excerpt with PDF page 2.
2. Click **Run LangGraph review** on the same intake to create a staff candidate. Check that the active snapshot ID stays the same. If the candidate passes the graph and backend checks, you can choose **Use reviewed candidate**, enter a reason, and see a new staff-adopted snapshot. Order creation still requires **Approve snapshot**.
3. Upload [PO-202-ambiguous.pdf](../output/pdf/milestone4/demo/PO-202-ambiguous.pdf) and refresh after processing. The PDF shows RM100.00 and RM90.00 as unselected options. Open the automatic trace, inspect the escalation and the unverified unit price, and check that approval is unavailable.

## Checks and paired evaluation

Run these commands from the repository root with PostgreSQL running and the migrated database selected.

```powershell
$env:DATABASE_URL = 'postgresql+psycopg://po_demo:po_demo@127.0.0.1:55432/po_demo_m4'
$env:OLLAMA_URL = 'http://127.0.0.1:11434'
$env:OLLAMA_MODEL = 'qwen3:8b'
.\.venv\Scripts\python.exe -m alembic check
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m scripts.evaluate_milestone4 --output-dir tmp/milestone4-results
```

The evaluator verifies the frozen PDF hashes in the [held-out manifest](../output/pdf/milestone4/held-out/manifest.json), makes independent baseline and graph calls for the same pages, and checks the drafts in a read-only database transaction. Its outputs contain per-PDF scores and saved handoffs. The [paired results](evidence/milestone4/paired-results.md) and [raw traces](evidence/milestone4/paired-results.json) are synthetic demo evidence. The two walkthrough PDFs are outside the scored set.

Both pipelines read 85 of 86 populated fields correctly and supported 80 of 86 with verified evidence. Both routed 10 of 12 PDFs correctly and required 10 estimated source-grounding corrections. The graph's median time was 12.01 seconds, compared with 7.87 for the baseline. Three rereads proposed changes outside their assigned scope and one proposed an unsupported value. The backend rejected all four patches and kept the original proposals for staff review. Neither pipeline correctly left any of the four labeled missing or ambiguous values blank. Ollama reported Qwen3 running entirely on the GPU with 6.2 GB resident during the benchmark.

The Windows dashboard smoke test verified the worker's four-call PO-201 run, the saved staff candidate, and the blocked PO-202 approval. The recorded runs are in the [walkthrough traces](evidence/milestone4/walkthrough-traces.json).

The automatic route uses evidence problems such as an unverified unit price, no identified order lines, or unresolved evidence on a multipage PDF. A verified price mismatch alone follows normal business review. Each graph run allows one targeted reread and at most four total model calls. Additional graph calls use a 45-second timeout by default. The worker's default lease includes their time budget. `PO_GRAPH_AUTO=false` selects baseline-only intake. Staff-triggered runs are still available. [LangGraph state and conditional edges](https://docs.langchain.com/oss/python/langgraph/graph-api) · [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs)
