# Milestone 3 on Windows

Use the existing PO-104 fixtures and two demo Gmail addresses. The buyer mailbox sends the PO and receives the confirmation. The supplier mailbox is connected to n8n. Use the Telegram chat and dashboard on the same Windows device for the walkthrough.

## 1. Set up the demo

Start Docker Desktop and the Ollama Windows app. In PowerShell, create a local settings file and generate separate keys for n8n and the staff API.

```powershell
# Run from your own repository root.
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if (-not (Test-Path .env.milestone3)) {
    $demoStaffKey = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
    $demoIntegrationKey = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
    Get-Content .env.milestone3.example |
        ForEach-Object { $_ -replace '^PO_STAFF_KEY=$', "PO_STAFF_KEY=$demoStaffKey" -replace '^PO_INTEGRATION_KEY=$', "PO_INTEGRATION_KEY=$demoIntegrationKey" } |
        Set-Content -Encoding utf8 .env.milestone3
}
docker compose --profile n8n up -d --wait db n8n
docker compose exec -T db createdb -U po_demo po_demo_m3
```

Run `createdb` once for this database name. For a fresh repeat walkthrough, choose a new database name in that command and in `DATABASE_URL` and `PDF_STORAGE`. An existing Milestone 2 database can also be upgraded after stopping its old API and taking a backup. Revision 0003 queues previously pending and interrupted work and preserves already approved orders.

Open [n8n](http://localhost:5678) and complete the local owner setup. Leave the workflows unpublished until the credentials and destinations are configured. The n8n service is pinned to 2.48.7.

## 2. Configure Gmail, Telegram, and n8n

1. Create the **PO-DEMO-INTAKE** label in the supplier demo Gmail mailbox. In a Google Cloud project, enable the Gmail API and set up the OAuth consent screen. For an external app in Testing mode, add the supplier mailbox as a test user. Create a **Web application** OAuth client. Add the exact redirect URL displayed in n8n's **Gmail OAuth2 API** credential. For this Compose setup it is normally `http://localhost:5678/rest/oauth2-credential/callback`. Enter the client ID and secret in n8n, click **Sign in with Google**, and connect the supplier mailbox. [n8n Google OAuth setup](https://docs.n8n.io/integrations/builtin/credentials/google/oauth-single-service)
2. Find the Gmail label ID. In n8n, execute a temporary **HTTP Request** node with method GET, URL `https://gmail.googleapis.com/gmail/v1/users/me/labels`, and authentication **Predefined Credential Type → Gmail OAuth2 API**. Copy the `id` of the label whose `name` is `PO-DEMO-INTAKE`. [Gmail labels list API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.labels/list)
3. Create a dedicated demo bot with BotFather, create a **Telegram API** credential in n8n with its token, and send `/start` to the bot from your private demo chat. Obtain the chat ID from `message.chat.id` in the bot's `getUpdates` response. The following PowerShell command displays the chat ID. [n8n Telegram credentials](https://docs.n8n.io/integrations/builtin/credentials/telegram) · [Telegram getUpdates](https://core.telegram.org/bots/api#getupdates)

   ```powershell
   $demoBotSecret = Read-Host 'Demo Telegram bot token' -AsSecureString
   $demoBotToken = [System.Net.NetworkCredential]::new('', $demoBotSecret).Password
   (Invoke-RestMethod "https://api.telegram.org/bot$demoBotToken/getUpdates").result |
       Where-Object { $_.message.chat.type -eq 'private' } |
       ForEach-Object { $_.message.chat.id } |
       Select-Object -Last 1
   Remove-Variable demoBotSecret, demoBotToken
   ```

4. In n8n, create a **Header Auth** credential. Use header name `X-Integration-Key` and copy the value of `PO_INTEGRATION_KEY` from `.env.milestone3`. Import [Gmail PO intake](../n8n/gmail-intake.json) and [Notification delivery](../n8n/notification-delivery.json) via **Import from File**. Assign credentials to these nodes.

   | Node | Credential or setting |
   | --- | --- |
   | **Intake settings** | Replace `Label_REPLACE_ME` with the copied Gmail label ID. |
   | **List labeled Gmail messages**, **Read Gmail message**, **Read attachment bytes**, **Remove processed intake label** | Supplier's **Gmail OAuth2 API** credential |
   | **Accept PDF in FastAPI**, **Claim notification**, **Report result to FastAPI** | **Header Auth** credential |
   | **Send Telegram alert** | Private demo **Telegram API** credential |
   | **Send Gmail confirmation** | Supplier's **Gmail OAuth2 API** credential |

5. Edit `.env.milestone3`. Replace the example address and chat ID below with the buyer's test Gmail address and your private demo chat ID. Each email mapping takes one bare address. The worker and API read the same settings. Leave `PO_OUTBOUND_ENABLED=false` for a credential-free run. Set it to `true` only when you are ready for live delivery.

   ```dotenv
   PO_GMAIL_SENDERS={"your-buyer-test@gmail.com":"CUST-101"}
   PO_GMAIL_RECIPIENTS={"CUST-101":"your-buyer-test@gmail.com"}
   PO_TELEGRAM_CHAT_ID=123456789
   PO_OUTBOUND_ENABLED=true
   ```

## 3. Start API, worker, and dashboard

Use three PowerShell windows. The API listens on port 8003 so Docker Desktop can reach it through `host.docker.internal`. The staff API and n8n routes require their respective keys when started below. The dashboard and n8n editor listen on loopback.

**API window**

```powershell
# Run from your own repository root.
. .\scripts\use_milestone3_env.ps1
if (-not $env:PO_STAFF_KEY -or -not $env:PO_INTEGRATION_KEY) { throw 'Set both demo keys first.' }
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m po_intake.seed
.\.venv\Scripts\python.exe -m uvicorn po_intake.api:app --host 0.0.0.0 --port 8003
```

**Worker window**

```powershell
# Run from your own repository root.
. .\scripts\use_milestone3_env.ps1
.\.venv\Scripts\python.exe -m po_intake.worker --fixture-replay
```

`--fixture-replay` produces repeatable drafts from the shipped PO-104 and PO-105 PDF text. Other PDFs go through Ollama. Omit the flag to use live Ollama on every text PDF. The normal worker allows three attempts per automatic cycle and uses a lease of the Ollama timeout plus 30 seconds. **Retry extraction** starts a new cycle on the same receipt and stored bytes.

**Dashboard window**

```powershell
# Run from your own repository root.
. .\scripts\use_milestone3_env.ps1
.\.venv\Scripts\python.exe -m streamlit run dashboard.py --server.address 127.0.0.1 --server.port 8503 --server.headless true
```

Open the [dashboard](http://127.0.0.1:8503). With the API running, verify container connectivity from another PowerShell window.

```powershell
docker compose --profile n8n exec -T n8n node -e "fetch('http://host.docker.internal:8003/health').then(async r=>{if(!r.ok)throw Error(r.status);console.log(await r.text())}).catch(e=>{console.error(e);process.exitCode=1})"
```

Expect `{"status":"ok"}`. If Windows prompts for firewall access, use your private demo network. Keep the two keys in the ignored local file and the n8n credential store. [Docker Desktop host networking](https://docs.docker.com/desktop/features/networking/)

## 4. Run the two-message live demo

1. Publish both workflows in n8n. In the supplier mailbox apply the intake label to a message from the configured buyer with [PO-104 V1](../output/pdf/PO-104-V1.pdf) attached. Intake polls every minute. Once FastAPI accepts every PDF attachment in the message, n8n removes the intake label.
2. Wait for the private Telegram alert before emailing V2. Open its dashboard link on the Windows demo device. Show the verified RM90.00 versus the agreed RM100.00 and the blocked approval. Under **Notifications**, expect **Telegram · Sent** with one attempt and a provider receipt.
3. Send and label a second buyer email with [PO-104 V2](../output/pdf/PO-104-V2.pdf). Refresh the dashboard, open the V2 receipt, compare the diff, enter a reason, and click **Activate revision**. Check the review box and click **Approve snapshot**. Note the sales order number.
4. The outbound poll runs every 15 seconds. In the buyer mailbox, check for one Gmail confirmation with both PO-104 and the sales order number. Refresh the dashboard and check **Gmail · Sent** with its provider receipt.
5. To check replay, reapply the label to the original V2 message. It should return the original intake ID. If you send the same V2 PDF in a new message, it should create a new **Exact resend** receipt linked to V2. Confirm there is still one sales order and one Gmail confirmation intent.

The dispatch workflow retries the result POST. Each claimed attempt runs its provider-send node once. Explicit provider rejection can be eligible for up to three automatic attempts. An inconclusive response or expired claim becomes **Unknown**. The **Notifications** panel lets staff record reconciliation after checking the provider and separately authorize a resend if they confirm non-delivery.

### Show unknown delivery without contacting a provider

Use a fresh demo database and keep the n8n delivery workflow unpublished. Start FastAPI with a configured demo recipient and `PO_OUTBOUND_ENABLED=true`, and set `PO_NOTIFICATION_LEASE_SECONDS=5` in the API window before startup. Upload only the clean V2 PDF through the dashboard, wait for extraction, and approve. From another PowerShell window, claim the Gmail intent and leave it without a result.

```powershell
# Run from your own repository root.
. .\scripts\use_milestone3_env.ps1
$demoHeaders = @{ 'X-Integration-Key' = $env:PO_INTEGRATION_KEY }
$demoClaim = Invoke-RestMethod 'http://127.0.0.1:8003/integrations/notifications/claim' -Method Post -Headers $demoHeaders
$demoClaim.delivery.channel
Start-Sleep -Seconds 6
```

Refresh the approved intake. **Gmail · Unknown** should appear alongside the confirmed sales order. The workflow itself remains paused for this simulation.

## Checks and live status

With PostgreSQL running, the tests create temporary schemas and PDF directories. Use the migrated demo settings for the standalone schema check.

```powershell
# Run from your own repository root.
. .\scripts\use_milestone3_env.ps1
.\.venv\Scripts\python.exe -m alembic check
.\.venv\Scripts\python.exe -m pytest -q
docker compose --profile n8n config --quiet
```

For a credential-free import check, use a disposable n8n user folder.

```powershell
docker compose --profile n8n run --rm --no-deps -e N8N_USER_FOLDER=/tmp/meridian-import-check n8n import:workflow --separate --input=/demo-workflows
```

Verified on Windows with no provider credentials. All 34 tests passed, both exports imported into n8n 2.48.7, and the workflow graphs ran against local mock providers over `host.docker.internal`. The mock Telegram endpoint rejected one requested send and accepted its retry. The mock Gmail endpoint accepted one confirmation and withheld its acknowledgement. The dashboard showed **Unknown** alongside the confirmed order, and another poll left the send count at one. The new worker also processed and approved PO-103 through real local Ollama.

Live verification is pending for Gmail OAuth and intake, the private Telegram alert and link on the intended device, and delivery of the Gmail confirmation to the test mailbox.
