# Milestone 5 Windows demo

## Start a fresh local demo

Start Docker Desktop and the Ollama Windows app. Use a Python 3.12 virtual environment. Run the following from the repository in PowerShell. If the environment already exists, skip the first Python command.

```powershell
# Run from your own repository root.
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
docker compose up -d --wait db
$demoDb = 'po_demo_m5_' + (Get-Date -Format yyyyMMdd_HHmmss)
docker compose exec -T db createdb -U po_demo $demoDb
$env:DATABASE_URL = "postgresql+psycopg://po_demo:po_demo@127.0.0.1:55432/$demoDb"
$env:PDF_STORAGE = "data/$demoDb/pdf"
$env:PO_MCP_API_URL = 'http://127.0.0.1:8005'
$env:PO_SOP_INDEX = 'data/milestone5/sop-index.json'
$env:PO_STAFF_KEY = ''
$env:PO_OUTBOUND_ENABLED = 'false'
$env:OLLAMA_URL = 'http://127.0.0.1:11434'
$env:OLLAMA_MODEL = 'qwen3:8b'
ollama list
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m po_intake.seed
.\.venv\Scripts\python.exe -m po_intake.sop_index
.\.venv\Scripts\python.exe -m uvicorn scripts.milestone5_demo:app --host 127.0.0.1 --port 8005
```

If the model is absent, run `ollama pull qwen3:8b` before starting the API. The demo launcher replays fixture drafts for three exact synthetic PDFs and uses the normal evidence, catalog, intake and revision logic. It disables automatic graph review and outbound delivery for this walkthrough. The chat planner uses live Ollama. FastAPI starts the local MCP server as a stdio subprocess for every chat turn, so the dashboard and API are the two PowerShell windows for this demo. The standard worker remains available when running the regular API with background extraction.

In the second PowerShell window, generate and upload the PDFs, run the frozen evaluation, and launch Streamlit.

```powershell
# Run from your own repository root.
$env:PO_API_URL = 'http://127.0.0.1:8005'
$env:PO_MCP_API_URL = $env:PO_API_URL
$env:PO_STAFF_KEY = ''
$env:PO_FOREGROUND_EXTRACTION = 'true'
.\.venv\Scripts\python.exe -m scripts.milestone5_demo
.\.venv\Scripts\python.exe -m scripts.evaluate_milestone5
.\.venv\Scripts\python.exe -m streamlit run dashboard.py --server.address 127.0.0.1 --server.port 8505 --server.headless true
```

The generated PDFs are in `data/milestone5/fixtures`. The seed activates `PO-106-V2.pdf` through the staff revision route and leaves the PO awaiting approval. `PO-106-V1.pdf` shows the earlier RM90.00 price. `PO-107-V1.pdf` shows the separate RM95.00 case.

For a standalone MCP probe, use another PowerShell prompt in the repository while the API is running. It starts the same stdio server, discovers the tool, and prints the real arguments and result.

```powershell
# Run from your own repository root.
$env:PO_MCP_API_URL = 'http://127.0.0.1:8005'
$env:PO_STAFF_KEY = ''
$receipt = (Invoke-RestMethod "$env:PO_MCP_API_URL/intakes") | Where-Object filename -eq 'PO-106-V2.pdf' | Select-Object -First 1
$detail = Invoke-RestMethod "$env:PO_MCP_API_URL/intakes/$($receipt.id)"
.\.venv\Scripts\python.exe -m po_intake.mcp_client --po-id $detail.po.id
```

## Verify in the dashboard

Open the [dashboard](http://127.0.0.1:8505), select **PO-106-V2.pdf** under **Intake receipt**, then scroll to **PO review chat**.

1. Ask **“The price in V2 now matches. Under our SOP, what else do I need to check before approving?”**. The answer cites PO-106 V2, confirms the verified prices match, reports no unresolved issues, and shows **SOP-02 rev 1 §3 · Pre-approval checks**. Expand or collapse the passage to inspect the four checks. Expand **MCP tool trace** to see the selected internal PO ID, active snapshot, latest candidate disposition, checked and expected prices, and UTC read time.
2. Ask **“Which unit price and revision are active on this PO?”**. The answer shows PO-106 V2, MYR 100.00 for BRG-6204, and MYR 45.50 for BELT-A42. This answer uses the live PO reference.
3. Ask **“Who can authorize a 15% price override?”**. The answer says that the indexed SOPs do not establish who can authorize a price override.
4. Leave PO-106 awaiting approval. To check isolation, select **PO-107-V1.pdf** and ask the price question again. The answer cites PO-107 V1, the verified MYR 95.00 price, and its mismatch against MYR 100.00.

## Method and evidence

The chat binds the selected internal PO ID in the route and again at the stdio server entry point. The single MCP tool reads the active snapshot through FastAPI in one repeatable-read, read-only transaction. It reruns the existing deterministic evidence and price checks on each turn. The trace's latest candidate field describes the highest document revision after V1 and its current disposition, including an activated or dismissed candidate.

The versioned SOP corpus is indexed before startup. The `heading-term-idf-v1` baseline uses numbered sections, term aliases, at least two matched terms, three times the weight for a heading hit, and up to three hits. It hashes the normalized source text and verifies passages when loading. Three separate development paraphrases ranked the intended sections first, and a warehouse question yielded no hits. That was enough for this four-section corpus, so the demo uses a lexical index. See the [versioned SOP sources](../sops/) and [official MCP Python SDK documentation](https://py.sdk.modelcontextprotocol.io/).

Ollama proposes relevant fact and passage IDs. The backend checks those IDs and renders source-backed facts and exact SOP excerpts. Required prices and the applicable pre-approval checklist remain available if the model returns an invalid response or times out. The override and cross-PO refusals are deterministic. Streamlit resets the conversation when the selected PO or active snapshot changes.

The [frozen question set](../fixtures/milestone5-chat.json) separates three demo questions from four held-out grounding cases. The recorded run used MCP SDK 2.3.0 and Ollama 0.30.11 with `qwen3:8b` (`500a1f067a9f`), temperature 0, seed 42, 8,192 context tokens and a 240-token response limit. The index corpus hash was `93870e7abef0cf8af27d1b561bb92250e081206e5e160477469f67407c4d12f8`. The run captures the selected snapshots, read arguments and results, scores and retrieved text, answer plan status, visible claims, and final citations. [Recorded run](evidence/milestone5/chat-results.json)

| Synthetic run | Result | Evidence |
| --- | --- | --- |
| Demo questions | 3/3 | Correct matched V2 and exact checklist, live prices without a policy citation, and an abstention on authority |
| Held-out questions | 4/4 | Rejected the stale V2 premise, returned the other PO's own checked price, and abstained on cross-PO and unsupported questions |
| Model plans | 4/4 validated | All four turns that called Ollama returned a constrained plan |
| Claims audit | 0 invented claims in 7 answers | Reviewed the facts against the recorded snapshots, the two exact SOP citations, and all three abstentions |

Run the regression suite against the isolated database from a PowerShell window with its `DATABASE_URL` set. The PostgreSQL tests use temporary schemas. The Milestone 5 tests use real MCP stdio and a controlled model plan for reproducibility.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m alembic check
```
