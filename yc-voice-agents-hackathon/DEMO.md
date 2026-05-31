# End-to-End Demo — Voice Incident-Triage Agent

The full loop: **you call the agent → it investigates the incident with you →
it autonomously pages the on-call engineer (a real phone rings) → it applies the
code fix on your verbal OK → the dashboard shows it all.**

## What's already live
- **Agent `flower-bot` deployed on Pipecat Cloud** (Claude / Gradium STT+TTS), org `flexible-smelt-emerald-345`, status Ready.
- **Autonomous paging ON** (`ENABLE_OUTBOUND_CALL=1`); every page routes to `DEMO_PAGE_NUMBER=+17653506634` (Fardin).
- Twilio number to call from / dial in: **+1 385 218 6732**.

---

## 1. Talk to the agent — two ways

### A) Dial in (you call the number)
One-time Twilio setup (console, ~2 min — TwiML Bins are console-managed):
1. Twilio Console → **TwiML Bins** → Create → paste:
   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <Response>
     <Connect>
       <Stream url="wss://api.pipecat.daily.co/ws/twilio">
         <Parameter name="_pipecatCloudServiceHost" value="flower-bot.flexible-smelt-emerald-345"/>
       </Stream>
     </Connect>
   </Response>
   ```
2. Phone Numbers → **+1 385 218 6732** → Voice Configuration → "A call comes in" → **TwiML Bin** → select it → Save.
3. **Dial +1 385 218 6732** from any phone → you're talking to the agent.

### B) Agent calls you (outbound two-way, no setup)
```bash
cd server
CALL_MODE=twoway PIPECAT_SERVICE_HOST=flower-bot.flexible-smelt-emerald-345 \
  DEST=+1XXXXXXXXXX CONFIRM_CALL=1 \
  uv run --no-project --with tzdata python simulate_and_call.py
```
Use **your** phone for `DEST` (must differ from Fardin's +17653506634, which the agent pages).

---

## 2. The conversation (the full loop)
1. Agent greets: *"You've reached on-call triage. What's paging you?"*
2. You: *"Payments API is throwing 500s, started a few minutes ago."*
3. Agent investigates (alerts → deploys → logs → metrics), then says the root cause:
   *deploy abc123 exhausted the payments-db connection pool.*
4. Ask it to **page the on-call engineer** → it picks who's on-shift and **calls Fardin's phone for real**, reading the briefing. ☎️
5. Agent: *"I found it in db.py — want me to apply a one-line fix capping the pool?"*
   You: *"Yes."* → it applies the patch (`code_fix` event). Say **no** and it won't touch anything — the safety gate.
6. Agent briefs, wraps up, hangs up.

---

## 3. The dashboard (optional, live)
The deployed bot pushes events to a relay. For the dashboard to show a **live**
call, the relay must be reachable from the cloud:

```bash
# laptop, terminal 1 — relay + dashboard
cd server
DASHBOARD_DIR=../dashboard uv run --no-project --with fastapi --with uvicorn uvicorn relay:app --port 8080
# terminal 2 — expose the relay publicly (pick one):
cloudflared tunnel --url http://localhost:8080        # or:  tailscale funnel 8080
```
Then set the tunnel URL as the bot's `RELAY_URL` and redeploy:
```bash
# edit server/.env: RELAY_URL=https://<your-tunnel-host>
pc cloud secrets set flower-bot-secrets --file .env --skip && pc cloud deploy --build-dir . --dockerfile Dockerfile -y
```
Open `http://localhost:8080/` — the trace, the page, and the code-fix diff render live as the call happens.

**No tunnel? Replay the recorded run instead** (same visuals, no live call):
```bash
cd server
DASHBOARD_DIR=../dashboard RELAY_NDJSON=./events.ndjson uv run --no-project --with fastapi --with uvicorn uvicorn relay:app --port 8080
# another terminal:
RELAY_URL=http://localhost:8080 uv run --no-project python ../dashboard/play_fixture.py
```

---

## 4. Cekura (the eval centerpiece)
In Claude Code: `/plugin install cekura@cekura-skills` → connect the agent
(**provider = Pipecat**, Pipecat Cloud key `pk_…`, agent name `flower-bot`) →
`/cekura-report`. Author metrics + scenarios from `docs/cekura-eval-plan.md`.
> Note: with autonomous paging ON, Cekura test calls will place real pages. Set
> `ENABLE_OUTBOUND_CALL=0` in the secret set during eval runs if you don't want that.

---

## 5. Teardown (stop reserved-agent billing)
```bash
pc cloud agent delete flower-bot          # or set min_agents = 0 in pcc-deploy.toml and redeploy
```

## Architecture (one glance)
```
phone ──Twilio──► Pipecat Cloud: bot-claude ──► Gradium STT → Claude → Gradium TTS
                       │  tools: diagnostic · routing · remediation (approval-gated)
                       │  call_engineer ──Twilio REST──► ☎ on-call engineer (Fardin)
                       └─ events ──► relay ──SSE──► dashboard
   eval: Cekura ──(Pipecat provider)──► the deployed agent
```
