# Meridian PO Intake

The local dashboard stores purchase order PDFs, checks draft evidence and agreed prices, and turns a reviewed active snapshot into a PostgreSQL sales order.

Milestone 3 adds an extraction worker and n8n integration for Gmail intake, Telegram review alerts, and Gmail confirmations. Follow the [Milestone 3 Windows guide](docs/milestone-3.md) for startup, credentials, the PO-104 walkthrough, and credential-free checks.

Milestone 4 adds bounded LangGraph extraction review, saved handoffs, and staff candidate snapshots. The [Milestone 4 Windows guide](docs/milestone-4.md) covers startup, the multipage reread, the ambiguous PDF, and the paired evaluation.

## Start the Milestone 2 Windows demo

Start Docker Desktop and the Ollama Windows app. Use PowerShell from the project folder. This workspace already has a virtual environment. On a fresh checkout, create it with `python -m venv .venv` using a working Python 3.12 installation.

In the API window, create an isolated database and PDF directory for this walkthrough. The suffix lets you repeat the demo with clean data.

```powershell
# Run from your own repository root.
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
docker compose up -d --wait db
$demoDb = 'po_demo_m2_' + (Get-Date -Format yyyyMMdd_HHmmss)
docker compose exec -T db createdb -U po_demo $demoDb
$env:DATABASE_URL = "postgresql+psycopg://po_demo:po_demo@127.0.0.1:55432/$demoDb"
$env:PDF_STORAGE = "data/$demoDb/pdf"
$env:OLLAMA_URL = 'http://127.0.0.1:11434'
$env:PO_FOREGROUND_EXTRACTION = 'true'
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m po_intake.seed
ollama list
```

If `qwen3:8b` is absent, run `ollama pull qwen3:8b`. Launch the reproducible walkthrough in the same API window.

```powershell
.\.venv\Scripts\python.exe -m uvicorn scripts.milestone2_demo:app --host 127.0.0.1 --port 8002
```

This launcher replays known extraction responses for the shipped PO-104 and PO-105 source pages, including the deliberate PO-105 misread. PO-103 and other documents use real Ollama. For live extraction on every document, launch `po_intake.api:app` in place of `scripts.milestone2_demo:app`. Model excerpts that fail verification go to review.

In a second PowerShell window, start the dashboard.

```powershell
# Run from your own repository root.
$env:PO_API_URL = 'http://127.0.0.1:8002'
$env:PO_FOREGROUND_EXTRACTION = 'true'
.\.venv\Scripts\python.exe -m streamlit run dashboard.py --server.address 127.0.0.1 --server.port 8502 --server.headless true
```

Open the [dashboard](http://127.0.0.1:8502) or [API docs](http://127.0.0.1:8002/docs).

## Verify Milestone 2

1. **Clean PO.** Upload [PO-103.pdf](output/pdf/PO-103.pdf) with **Upload and extract**. Check the page 1 evidence for two `BRG-6204` at RM100.00 and three `BELT-A42` at RM45.50. Check the review box, click **Approve snapshot**, and reopen the order under **Sales orders**.
2. **Misread.** Upload [PO-105-misread.pdf](output/pdf/PO-105-misread.pdf). Its PDF says RM90.00 for `BRG-6204` and the replayed draft says RM100.00. Under **Correct a reading**, choose **Line 1 · Unit price**, enter `RM90.00` for **Value as printed**, leave **Source page** at `1`, use `BRG-6204 Deep groove bearing 2 EA RM90.00` for **Exact source excerpt**, and enter a reason. Click **Save correction**. Edit 2 shows verified RM90.00 and an agreed price of RM100.00, so approval stays blocked. **Original model output** and **Version, receipt, and edit history** retain the original reading.
3. **V1.** Upload [PO-104-V1.pdf](output/pdf/PO-104-V1.pdf). Confirm the RM90.00 unit price is already verified and approval is blocked by the agreed RM100.00 price. Keep this intake open in one browser tab. Before uploading V2, save its approval payload in a third PowerShell window.

   ```powershell
   $demoApi = 'http://127.0.0.1:8002'
   $v1Receipt = (Invoke-RestMethod "$demoApi/intakes") | Where-Object { $_.filename -eq 'PO-104-V1.pdf' } | Select-Object -First 1
   $v1 = Invoke-RestMethod "$demoApi/intakes/$($v1Receipt.id)"
   $v1Approval = @{ snapshot_id = $v1.snapshot.id } | ConvertTo-Json -Compress
   ```

4. **V2 and stale submission.** In a second browser tab, upload [PO-104-V2.pdf](output/pdf/PO-104-V2.pdf). Check the diff for `BRG-6204` from RM90.00 to RM100.00 and unchanged quantity. Enter a reason and click **Activate revision**. The stale V1 tab already had its approval button disabled because of the price mismatch. To verify server-side stale handling, submit the saved V1 snapshot from PowerShell and expect HTTP 409 with a refresh path.

   ```powershell
   try {
     Invoke-RestMethod "$demoApi/intakes/$($v1.id)/approve" -Method Post -ContentType 'application/json' -Body $v1Approval
   } catch {
     [int]$_.Exception.Response.StatusCode
     $_.ErrorDetails.Message
   }
   ```

   Refresh the original tab to see V1 marked **Superseded**. In the V2 tab, review and approve the active snapshot. The stored sales order shows V2.
5. **Resends and equivalent copy.** Upload [the V1 copy](output/pdf/PO-104-V1-copy.pdf) and [the V2 copy](output/pdf/PO-104-V2-copy.pdf). Each shows **Exact resend** with its own intake ID and a link to its original. Upload [the regenerated copy](output/pdf/PO-104-equivalent.pdf) and confirm **Equivalent copy** with the rows reordered and decimal formatting changed. The approved version and sales order number stay the same.
6. **Post-order and customer context.** Upload [the changed post-order PDF](output/pdf/PO-104-post-order.pdf). The proposed quantity is four, while the booked quantity stays two. The PDF appears as a **Post-order follow-up**. Then upload the V2 copy while selecting **Selangor Fabrication** as the optional customer context. The receipt goes to **Identity review** without a PO or order link. To resolve it, edit **Customer reference** to `CUST-101` using page 1 and `Customer reference: CUST-101` as the excerpt. Check **Confirm this source-supported customer for the intake**, give a reason, and save. It becomes an exact resend with its original context and correction recorded.

The [fixture directory](output/pdf/) is reproducible with `.\.venv\Scripts\python.exe scripts/generate_fixture.py`.

## Migration, failure recovery, and checks

For an existing Milestone 1 database, use its usual `DATABASE_URL` and `PDF_STORAGE` and run `.\.venv\Scripts\python.exe -m alembic upgrade head` and `.\.venv\Scripts\python.exe -m po_intake.seed`. Existing orders keep their order and snapshot IDs. Open migrated drafts can be assessed with **Recheck draft**.

To reproduce extraction failure in the same demo database, first generate a distinct PDF to avoid exact resend reuse.

```powershell
.\.venv\Scripts\python.exe -c "from pathlib import Path; from scripts.generate_fixture import generate; generate(Path('tmp/PO-106-retry.pdf'), 'PO-106')"
```

Stop the API with Ctrl+C, set `$env:OLLAMA_URL = 'http://127.0.0.1:1'`, and restart it with the same launch command. Upload the generated PDF. Stop the API, set `$env:OLLAMA_URL = 'http://127.0.0.1:11434'`, and restart again. Refresh the dashboard, open the failed receipt, and click **Retry extraction**. If a process stopped mid-call, confirm it exited before marking its attempt interrupted.

Run these checks with the Compose database running and `DATABASE_URL` pointing to the migrated demo database. You can use the same API window after stopping Uvicorn with Ctrl+C. Pytest creates isolated schemas and temporary PDF storage.

```powershell
docker compose config --quiet
.\.venv\Scripts\python.exe -m alembic check
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q
```

## Evidence and local settings

The verifier folds whitespace and line wrapping in excerpts, preserves punctuation and case, and saves the original character range. It checks labeled PO headers and tables with identifiable SKU, quantity, and unit-price columns. Repeated quotes and rows whose associations or layouts cannot be determined stay in review. The reviewer can select a more specific excerpt from the original page. The business key is the verified customer and the PO number with surrounding whitespace trimmed and case folded. A review decision records the expected active snapshot, and approval rechecks pending revisions and current prices under the PO lock.

PostgreSQL listens on port 55432 on loopback. PDF storage defaults to `data/pdf`. The API accepts `DATABASE_URL`, `PDF_STORAGE`, `OLLAMA_URL`, `OLLAMA_MODEL`, `OLLAMA_TIMEOUT` (seconds), and `DEMO_REVIEWER`. The dashboard uses `PO_API_URL` if the API location changes.

The baseline uses Ollama 0.30.11 and `qwen3:8b` (ID `500a1f067a9f`, Q4_K_M), with thinking disabled, temperature 0, seed 42, 8,192 context tokens, and a 180-second timeout. On the tested Windows laptop with an RTX 5050 Laptop GPU and 16 GB RAM, Ollama reported 100% GPU placement and 6.2 GB resident for Qwen3. [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs) · [Qwen3 model tag](https://ollama.com/library/qwen3:8b)

The Milestone 1 model comparison used two generated development PDFs. DEV-001 uses the two agreed prices and DEV-002 changes the BELT-A42 price to RM40.00. Both models produced schema-valid responses. Wall times include model loading when needed.

| Model | PDF | Correct labeled values | Literal quotes with token | Row-scoped quantity/price quotes | Wall time |
| --- | --- | --- | --- | --- | --- |
| Qwen3 8B | DEV-001 | 9/9 | 9/9 | 4/4 | 9.39 s |
| Qwen3 8B | DEV-002 | 9/9 | 9/9 | 4/4 | 8.70 s |
| Llama 3.2 | DEV-001 | 7/9 | 3/9 | 0/4 | 10.82 s |
| Llama 3.2 | DEV-002 | 7/9 | 3/9 | 0/4 | 5.19 s |
