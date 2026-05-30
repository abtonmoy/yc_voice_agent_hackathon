# LLD — Backend (Voice Agent + Tools + Event Relay)

Low-level design for the incident-triage agent backend. Built by forking the hackathon starter `bot-nemotron.py`. Three capabilities (triage → routing → remediation) plus an **event relay** that streams structured events to the dashboard (see `lld-frontend.md`).

---

## 1. Components

```
┌────────────────────────────────────────────────────────────────┐
│ VOICE BOT  (Pipecat — bot-nemotron.py, forked)                  │
│   STT (Nemotron Speech) → LLM (Nemotron-3-Super) → TTS (Gradium)│
│   tools: diagnostic · routing · remediation                     │
│   every tool call + lifecycle step → EventBus.emit(...)         │
└───────────────┬───────────────────────────────┬────────────────┘
                │ events (fire-and-forget HTTP)   │ outbound (Twilio REST)
                ▼                                 ▼
┌────────────────────────────┐          ☎ engineer's phone
│ EVENT RELAY  (FastAPI)     │
│  POST /events  (ingest)    │◄── bot pushes
│  GET  /stream  (SSE)       │──► dashboard subscribes
│  ring buffer + events.ndjson (replay/backup)                    │
└────────────────────────────┘
                ▲
   data: mock_backend.py (INCIDENTS, ENGINEERS) + sample-service/ git repo
   external: Cekura (drives bot via Pipecat Cloud; its own dashboard shows the curve)
```

**Why a relay (not the bot serving SSE directly):** decouples the dashboard from where the bot runs. Bot local over WebRTC, or deployed to Pipecat Cloud for a phone call — either way it pushes the same events to the relay, and the dashboard reads from one stable URL. Also gives us a replay log for the backup recording.

---

## 2. Module layout

```
server/
  bot-nemotron.py        # forked: pipeline + tool registration + event hooks
  bot-gpt.py             # GPT-4.1 A/B variant (same tools)
  mock_backend.py        # INCIDENTS, ENGINEERS (replaces BOUQUETS/KNOWN_CUSTOMERS)
  tools/
    diagnostic.py        # get_alerts, get_deploy_history, get_logs, get_metrics
    routing.py           # find_on_call_engineer, call_engineer
    remediation.py       # read_repo_file, propose_code_fix, apply_code_fix
  events.py              # EventBus + EventClient (push to relay)
  relay.py               # standalone FastAPI relay (run separately)
  latency.py             # Pipecat MetricsFrame tap → latency events
sample-service/          # the monitored app (separate git repo)
  app/{db.py, batch_jobs.py, tax_service.py}
  (git history incl. commits abc123, def456)
```

> Tools may be kept as nested functions inside `run_bot()` (the starter's pattern) or factored into `tools/` modules and registered the same way. The starter uses **direct functions** registered via `ToolsSchema(standard_tools=[...])` + `llm.register_direct_function(fn)`; the **docstring is the tool schema/description**. Keep that pattern.

---

## 3. Event schema (the contract with the frontend)

Single envelope; `payload` varies by `type`. `seq` is a per-session monotonic int for ordering + client-side dedupe.

```jsonc
{
  "id": "evt_000123",
  "seq": 123,
  "ts": 1748600000000,          // epoch ms, stamped in the bot
  "session_id": "sess_ab12",
  "type": "rca",
  "payload": { ... }
}
```

| `type` | payload | emitted when |
|---|---|---|
| `session_start` | `{transport, incident_id?}` | call/WebRTC connects |
| `transcript` | `{role:"user"\|"agent", text, final:bool}` | STT final / agent turn |
| `alert` | `{title, service, severity, started_at}` | `get_alerts` |
| `tool_call` | `{tool, args}` | any tool invoked |
| `tool_result` | `{tool, summary}` | tool returns (synthesized string) |
| `hypothesis` | `{text, confidence}` | agent forms a ranked hypothesis |
| `rca` | `{root_cause, evidence, proposed_remediation, code_fixable:bool}` | convergence |
| `routing_decision` | `{chosen:{id,name,location,local_time}, reason, candidates:[{name,local_time,on_shift,teams,excluded_reason}]}` | `find_on_call_engineer` |
| `outbound_call` | `{engineer, phone_masked, status:"ringing"\|"answered"\|"ended"}` | `call_engineer` lifecycle |
| `fix_proposed` | `{incident_id, file, diff, summary}` | `propose_code_fix` |
| `fix_decision` | `{approved:bool}` | engineer says yes/no |
| `code_fix` | `{file, before, after, diff, applied:bool}` | `apply_code_fix` |
| `latency` | `{metric:"stt"\|"llm_ttft"\|"tts_first"\|"v2v", ms}` | Pipecat metrics tap |
| `session_end` | `{summary}` | disconnect |

**`code_fix` carries the full `before`/`after`/`diff` text** → the dashboard renders the change with zero filesystem access, even when the bot is in a cloud container.

---

## 4. Data layer (`mock_backend.py`)

```python
INCIDENTS = {
  "inc-1": {
    "alert": {"title": "payments-api 5xx >20%", "service": "payments-api",
              "severity": "P1", "started_at": "14:32"},
    "deploys": [{"id": "abc123", "service": "payments-api", "at": "14:30"}],
    "logs": ["payments-api: FATAL remaining connection slots reserved → payments-db (200× from 14:32)"],
    "metrics": ["payments-db active_connections 100/100 @14:31"],
    "affected_team": "payments",
    "ground_truth_root_cause": "deploy abc123 (14:30) exhausted the payments-db connection pool; errors began 14:32",
    "proposed_remediation": "roll back abc123 / cap pool size",
    "fix": {                       # None for non-code incidents (e.g. inc-3 cert)
      "code_fixable": True,
      "file": "app/db.py",
      "before": "engine = create_engine(DB_URL)            # no pool cap (abc123)",
      "after":  "engine = create_engine(DB_URL, pool_size=20, max_overflow=0)"
    }
  },
  # inc-2 batch_jobs.py (no deploy) · inc-3 cert (fix=None, ops) · inc-4 tax_service.py (def456)
}

ENGINEERS = {
  "priya": {"name":"Priya","phone":"+44...","location":"London",
            "timezone":"Europe/London","working_hours":[9,18],"teams":["payments","database"]},
  # lena/Berlin networking · raj/Bangalore database · mei/Singapore infra ·
  # sam/NY payments · diego/SF frontend   (see cekura-eval-plan.md §1a)
}
```

The `fix.before/after` strings are the source of truth for the patch (deterministic). The `sample-service/` repo holds the real, runnable file for `read_repo_file` realism; `get_deploy_history` reads its `git log` so deploys `abc123/def456` are genuine commits.

---

## 5. Tools spec

All tools: `async def tool(params: FunctionCallParams, ...) -> None`, return via `await params.result_callback({...})`, and call `events.emit(...)` for `tool_call` + a domain event. Docstrings are the LLM-facing schema.

### 5.1 Diagnostic (`tools/diagnostic.py`)
| tool | args | returns (summary string) | emits |
|---|---|---|---|
| `get_alerts()` | — | active alert summary | `alert`, `tool_*` |
| `get_deploy_history(service?)` | service | recent deploys (`git log` of sample repo) | `tool_*` |
| `get_logs(service)` | service | pre-correlated log summary | `tool_*` |
| `get_metrics(name)` | name | key series snapshot | `tool_*` |

Each returns a **synthesized summary**, never raw rows. (See `cekura-eval-plan.md` for ground-truth strings.)

### 5.2 Routing (`tools/routing.py`)
```python
async def find_on_call_engineer(params, incident_area: str) -> None:
    """Find the engineer to page: must be ON-SHIFT now (their local working
    hours) AND own the affected system (team match). Returns the best person
    + one backup, each with a reason. Filter by working hours FIRST, then expertise."""
    now = _demo_now()                      # DEMO_NOW env override, else real now
    ranked, candidates = [], []
    for eid, e in ENGINEERS.items():
        local = now.astimezone(ZoneInfo(e["timezone"]))
        on_shift = e["working_hours"][0] <= local.hour < e["working_hours"][1]
        match = incident_area in e["teams"]
        candidates.append({... "on_shift": on_shift, "excluded_reason":
                           None if (on_shift and match) else ("asleep" if not on_shift else "wrong team")})
        if on_shift and match: ranked.append((eid, e, local))
    chosen = ranked[0] if ranked else _fallback(...)   # nearest same-team awake / state none
    emit("routing_decision", {chosen, reason, candidates})
    await params.result_callback({chosen, reason})

async def call_engineer(params, engineer_id: str, briefing: str) -> None:
    """Place an OUTBOUND call to the chosen engineer and brief them. Only call
    after find_on_call_engineer has chosen someone."""
    # Twilio REST calls.create(to=phone, from=TWILIO_NUMBER, twiml=<Connect><Stream wss.../>)
    # emit outbound_call ringing→answered→ended  (see §7)
```
- `_demo_now()`: parse `DEMO_NOW` (ISO8601 w/ offset) → fixed clock; else `datetime.now(timezone.utc)`.
- Timezones via stdlib `zoneinfo` (no deps).

### 5.3 Remediation (`tools/remediation.py`) — **approval-gated, two-step**
```python
_pending = {}   # session_id -> {"incident_id","file","before","after","diff"}

async def read_repo_file(params, path: str) -> None:
    """Read a file from the monitored service repo (read-only)."""
    ...

async def propose_code_fix(params, incident_id: str) -> None:
    """Propose (do NOT apply) the fix for an incident. Returns a diff to read
    aloud and ASK the engineer for approval. Never applies anything."""
    fix = INCIDENTS[incident_id].get("fix")
    if not fix or not fix["code_fixable"]:
        await params.result_callback({"code_fixable": False,
            "advice": INCIDENTS[incident_id]["proposed_remediation"]})  # e.g. cert rotation
        return
    diff = unified(fix["before"], fix["after"], fix["file"])
    _pending[params.session_id] = {**fix, "incident_id": incident_id, "diff": diff}
    emit("fix_proposed", {incident_id, "file": fix["file"], diff, "summary": ...})
    await params.result_callback({"diff": diff, "needs_approval": True})

async def apply_code_fix(params, engineer_approved: bool) -> None:
    """Apply the previously proposed fix. ONLY call this after the engineer has
    explicitly said yes in the conversation."""
    p = _pending.get(params.session_id)
    if not p or engineer_approved is not True:           # HARD GATE
        emit("fix_decision", {"approved": False})
        await params.result_callback({"applied": False, "reason":
            "no approved pending fix — refusing"})
        return
    _write_file(p["file"], full_after(p))                # local write
    emit("fix_decision", {"approved": True})
    emit("code_fix", {"file": p["file"], "before": p["before"],
                      "after": p["after"], "diff": p["diff"], "applied": True})
    _pending.pop(params.session_id, None)
    await params.result_callback({"applied": True})
```
**The gate is in code, not just the prompt:** `apply_code_fix` writes nothing unless (a) a pending proposal exists from a *prior* `propose_code_fix` turn and (b) `engineer_approved is True`. A single tool never both decides and applies. This satisfies the non-negotiable invariant + Cekura metric T4.

---

## 6. Event system (`events.py`)

```python
class EventBus:
    def __init__(self, session_id, client): self.seq=0; ...
    def emit(self, type, payload):
        evt = {"id": f"evt_{self.seq:06d}", "seq": self.seq, "ts": now_ms(),
               "session_id": self.session_id, "type": type, "payload": payload}
        self.seq += 1
        asyncio.create_task(self.client.push(evt))   # fire-and-forget, never blocks voice path
```
- `EventClient.push`: `httpx.post(RELAY_URL+"/events", json=evt, headers={"X-Relay-Token":...})`, wrapped in try/except — **a relay outage must never affect the call.**
- One `EventBus` per session, created in `run_bot()`, closed over by the tools (same pattern as the starter's `order` dict). Pass `session_id` from the transport.

---

## 7. Outbound calling (Twilio)

```
find_on_call_engineer → chosen.phone
call_engineer:
  Twilio REST calls.create(
     to=chosen.phone, from_=TWILIO_NUMBER,
     twiml="<Connect><Stream url='wss://api.pipecat.daily.co/ws/twilio'>
              <Parameter name='_pipecatCloudServiceHost' value='flower-bot.ORG'/>
            </Stream></Connect>")
  → emit outbound_call {status:"ringing"} ; on Twilio status callback → answered/ended
```
- Reuses the **same `wss://` path** as inbound — Twilio just *originates*. *(Verify Pipecat Cloud dial-out specifics with mentors; if blocked, fall back to announce-only: skip `call_engineer`, the `routing_decision` event still drives the demo.)*
- Demo: `chosen.phone` for the one live-dialed engineer = **your own controlled phone**.

---

## 8. Latency capture (`latency.py`)

The forked pipeline already sets `PipelineParams(enable_metrics=True)` and uses `VLLMOpenAILLMService` (reports TTFB to first non-thinking token). Add a lightweight processor between stages that taps `MetricsFrame`s and `emit("latency", {...})` for `llm_ttft` / `tts_first`; compute `v2v` as user-turn-end → first-audio-out. *(Exact MetricsFrame fields vary by Pipecat version — verify; worst case, time the LLM service call directly.)*

---

## 9. Relay service (`relay.py`)

```python
buffer = deque(maxlen=500)
subscribers: set[asyncio.Queue] = set()

@app.post("/events")        # auth via X-Relay-Token
async def ingest(evt: dict):
    buffer.append(evt); append_ndjson("events.ndjson", evt)
    for q in subscribers: q.put_nowait(evt)

@app.get("/stream")         # SSE
async def stream():
    q = asyncio.Queue(); subscribers.add(q)
    async def gen():
        for e in list(buffer): yield sse(e)      # replay backlog to late joiners
        while True: yield sse(await q.get())
    return EventSourceResponse(gen())

@app.post("/reset")         # clear between demo runs
@app.get("/replay")         # stream events.ndjson with original timing (backup demo)
```
- In-memory, single process — fine for a hackathon. Run with `uvicorn relay:app --port 8080`.
- **Demo topology (chosen): live Twilio call → bot on Pipecat Cloud, dashboard on the laptop.**
  - Relay runs **on the laptop** (`:8080`). The dashboard reads `http://localhost:8080/stream` — local, instant.
  - The Pipecat Cloud bot is remote, so it needs a **public** ingest URL: put a tunnel in front of the relay (`cloudflared tunnel --url http://localhost:8080` or `ngrok http 8080`) and set the bot's `RELAY_URL` (in Pipecat Cloud secrets) to the tunnel URL. Only `POST /events` traverses the tunnel; the dashboard stays on localhost.
  - `X-Relay-Token` guards `/events` (it's now a public surface). Tunnel latency is irrelevant — events are fire-and-forget and off the voice path.
  - *Alt:* host the relay on a tiny always-on box and point both bot and dashboard at it. More robust, one more deploy — only if the tunnel is flaky.

---

## 10. Config / env

```
# models (provided)
NVIDIA_ASR_URL, NEMOTRON_LLM_URL, NEMOTRON_LLM_MODEL=nvidia/nemotron-3-super
GRADIUM_API_KEY, GRADIUM_VOICE_ID
OPENAI_API_KEY                      # bot-gpt.py A/B
# telephony
TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_NUMBER
# our additions
RELAY_URL=http://localhost:8080    # bot→relay. On Pipecat Cloud, set to the tunnel URL (see §9)
RELAY_TOKEN=<shared secret>        # guards the public POST /events
DEMO_NOW=2026-05-30T03:00:00-07:00  # deterministic routing clock (unset = real now)
SAMPLE_REPO_PATH=../sample-service
```

---

## 11. Headline sequence (E1, end-to-end)

```
caller connects ─► session_start
"payments API throwing 500s"
  → get_alerts        → alert, tool_result
  → get_deploy_history→ tool_result (abc123 @14:30)
  → get_logs/metrics  → tool_result (pool 100/100 @14:32)
  → agent: hypothesis → rca {code_fixable:true}
  → find_on_call_engineer → routing_decision (chosen: priya; diego asleep, sam pre-shift)
  → call_engineer     → outbound_call ringing→answered     [your phone rings]
  → propose_code_fix  → fix_proposed (db.py diff)          [diff appears, pending]
  agent: "apply it?"  ; engineer: "yes"
  → apply_code_fix(true) → fix_decision{approved} + code_fix{applied}  [diff highlights]
"done — here's the change" ─► session_end
```

---

## 12. Failure modes / fallbacks
| Failure | Fallback |
|---|---|
| Nemotron endpoint down | switch to `bot-gpt.py` (same tools) |
| Twilio outbound blocked | announce-only: skip `call_engineer`; `routing_decision` still drives UI |
| Relay down | call is unaffected (fire-and-forget); dashboard just goes stale |
| Live patch flaky | propose-only: `fix_proposed` renders, skip `apply` |
| Cekura can't reach agent | it tests the **deployed** agent over WebRTC via Pipecat Cloud API — independent of Twilio |
```
