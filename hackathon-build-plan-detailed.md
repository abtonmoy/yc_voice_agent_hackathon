# Voice Incident-Triage Agent — Detailed Action Plan (v2, reconciled with official repo)

Companion to the task breakdown. Every task has: **Actions** (what to do), **Watch** (the gotcha most likely to bite), and **DONE WHEN** (the gate). Commands marked *(verify)* are best-known but versions move — confirm on the day.

**The project:** the README ships a working **Field & Flower** flower-shop bot (Pipecat). We *do not* build from scratch and we *do not* keep the flower shop — we **fork that bot into an on-call incident-triage agent**. Two layers:

- **Layer 1 — Triage:** you phone it when paged with no laptop; it calls diagnostic tools over correlated incident fixtures, converges on a root cause out loud, and pushes you an RCA. Same pipeline, new domain.
- **Layer 2 — Smart on-call routing (the autonomous act):** an incident fires, the agent triages it, then looks up an **engineer directory**, figures out who's actually *on-shift right now* (location → timezone → local working hours) **and** owns the affected system (team/expertise match), and **places a real outbound call** to that engineer to brief them. Follow-the-sun paging: never wake the person it's 3 AM for. The routing decision is itself Cekura-scorable, which ties this layer straight into the judging centerpiece.

**Golden rule:** if a step isn't on the critical path (⛔) and it's fighting you, skip it and move on. Working-and-narrow beats broad-and-broken.

---

## What changed from v1 of this plan (read once)

The official repo (`github.com/pipecat-ai/yc-voice-agents-hackathon`) removed our three biggest risks:

- **No self-hosted inference.** Models are hosted on AWS; you point env vars at given URLs. The vLLM / Nemotron-Nano / Mamba-hybrid risk is **gone**. (LLM is now **Nemotron 3 Super 120B**, hosted.)
- **No tunnel/TLS fight.** Deploy target is **Pipecat Cloud**; the Twilio TwiML Bin points at `wss://api.pipecat.daily.co/ws/twilio`. The public `wss://` is handled — you just configure Twilio.
- **Two starters, not one.** `bot-gpt.py` (GPT-4.1) and `bot-nemotron.py` (Nemotron). Nemotron is our primary (judging wants NVIDIA OSS models); GPT is both the fallback **and** a Cekura A/B comparison beat.
- **TTS is Gradium** (not Magpie). **Cekura runs from Claude Code** via its MCP plugin (`/cekura-report`, provider = `Pipecat`).

**Provided stack (V2 — our primary):**
```
NVIDIA_ASR_URL=ws://44.241.251.184:8080            # Nemotron Speech Streaming (STT)
NEMOTRON_LLM_URL=http://nemotron-fleet-alb-1322439314.us-west-2.elb.amazonaws.com/v1
NEMOTRON_LLM_MODEL=nvidia/nemotron-3-super         # Nemotron 3 Super 120B
# TTS = Gradium (GRADIUM_API_KEY).  Transports: SmallWebRTC (local) + Twilio (phone).
```
**V1 (fallback / comparison):** GPT-4.1 via OpenAI Responses API + Gradium STT/TTS (`OPENAI_API_KEY`, `GRADIUM_API_KEY`).

---

## Standing principle — local first, then cloud, then phone

The README's own iteration order, and it kills most integration pain:

1. **Get a voice round-trip locally over WebRTC** (`localhost:7860`) before touching the cloud or the phone. Fastest loop.
2. **Then `pc cloud deploy`** to Pipecat Cloud once the agent is good.
3. **Then wire Twilio** (TwiML Bin → `wss://api.pipecat.daily.co/ws/twilio`).

The public secure-WebSocket endpoint is **provided by Pipecat Cloud** — you don't stand up TLS yourself. The hosted model stays private to your bot; only Pipecat Cloud's transport is public. Measure your headline latency on the **deployed** path, not on localhost.

---

## Official schedule (hard anchors)

- **9:00** — hackathon begins
- **12:00** — lunch
- **6:00 PM** — submissions due
- **6:00–8:00** — demos · **8:00** — judges' presentations

Derived internal anchors: **Freeze 3:30 · Record backup 5:15 · Submit 5:45** (15-min buffer — don't ride 6:00).

---

## Phase 0 — Setup (9:00–10:00) · de-risk only

### S1 — Accounts + credits
**Actions:**
1. Log into: Cekura (`dashboard.cekura.ai` — credits auto-apply for approved hackers), Gradium (`gradium.ai` — credit code given on-site), OpenAI, Twilio (`twil.io/yc-hack` for credits), Pipecat Cloud (`pipecat.daily.co/sign-up`).
2. One `.env` scratch: `OPENAI_API_KEY`, `GRADIUM_API_KEY`, `TWILIO_*` (SID/token + a voice number), plus the three NVIDIA endpoint vars above. (No NVIDIA key needed — the endpoints are open to the event.)
3. Install the Pipecat CLI and log in: `uv tool install pipecat-ai-cli && pc cloud auth login`.

**Watch:** Twilio trial restrictions (verified caller IDs) — provision a voice-capable number now, add credits early.
**DONE WHEN:** logged into all five; `.env` populated; `pc cloud auth login` succeeds.

### S2 ⛔ — Clone the official repo, run the stock bot
**Actions:**
1. `git clone https://github.com/pipecat-ai/yc-voice-agents-hackathon.git && cd yc-voice-agents-hackathon/server`
2. `cp .env.example .env` and fill keys. `uv sync`.
3. `uv run bot-nemotron.py` → open `http://localhost:7860` → **Connect** → talk to Field & Flower. (First launch ~20s: downloads VAD + turn-detection models.)
4. Skim `bot-nemotron.py` and `bot-gpt.py`: find the **system prompt**, the **tool/function definitions** (catalog lookup, capture delivery, place order), and the **pipeline assembly** (ASR → LLM → TTS). These three are what you'll edit.

**Watch:** this is a *working* bot — resist rewriting it. You are swapping the prompt + tools, not the plumbing.
**DONE WHEN:** you talk to the stock flower bot over WebRTC and it replies, **and** you can point to where the prompt + tools live.

### S3 ⛔ — Both starters run; pick the primary
**Actions:**
1. `uv run bot-gpt.py` too — confirm GPT-4.1 path also does a voice round-trip.
2. **Primary = `bot-nemotron.py`** (judging favors NVIDIA OSS). Keep `bot-gpt.py` as fallback + the Cekura A/B comparison.

**Watch:** if Nemotron-120B is noticeably slower/flakier than GPT for tool-calling, note it now — that's data for the latency/quality slide, not a reason to drop it.
**DONE WHEN:** both bots verified to round-trip; Nemotron chosen as primary.

### S4 🔀 — Trace-view source decision
**Actions:**
1. Run the Pipecat dev UI / any debug view against the stock bot; trigger a tool call.
2. Check whether tool calls + results render **legibly from the back of a room**.

**DONE WHEN:** Yes/No recorded. Yes → D1 is free. No → D1 = build a tiny log tailer (Phase 4).

**🔀 GATE @ 10:00:** Nemotron round-trips → primary stays Nemotron. Nemotron path broken and not fixed fast → primary = GPT-4.1, keep Nemotron as the "we also ran the OSS model" beat. **Pipeline stays alive either way — this gate is now low-stakes** (both are one `uv run` away).

---

## Phase 1 — Make it an incident agent (10:00–12:00)

You're editing Field & Flower into the triage bot. Work in **text mode first** where you can.

### I1 ⛔ — Fixture schema (one incident)
**Actions:**
1. JSON per incident: `alert` (what paged you), `deploys` (recent timeline), `logs` (a few pre-correlated lines), `metrics` (key series snapshots), and **`ground_truth_root_cause`** (string + the deploy/metric that proves it).
2. Author incident #1 = "bad deploy exhausts DB connection pool." Timestamps must line up (deploy 14:30 → errors 14:32).
3. Keep logs/metrics small and already-correlated — tools return a *summary*, not a 4KB dump.

**Watch:** `ground_truth_root_cause` is what makes Cekura scoring possible — don't skip it.
**DONE WHEN:** incident #1 JSON validates and contains ground truth.

### I2 ⛔ — Mock tools (replace the flower-shop tools)
**Actions:**
1. In `bot-nemotron.py`, swap the catalog/delivery/order functions for `get_alerts()`, `get_deploy_history()`, `get_logs(service)`, `get_metrics(name)` reading the fixture.
2. Each returns a **synthesized summary string** ("200× connection-refused to payments-db starting 14:32, 2 min after deploy abc123"), not raw rows.
3. Re-register the new tool schemas with the LLM service (same registration spot the flower tools used).

**Watch:** tool *descriptions* drive accuracy more than anything — make them unambiguous and say when to use each. This is the lever Cekura's loop will tune. Keep the function-calling mechanism identical to the starter's (don't reinvent it).
**DONE WHEN:** each tool is callable in the pipeline and returns a pre-correlated summary.

### I3 ⛔ — Triage system prompt (replace the flower prompt)
**Actions:**
1. Role = on-call triage assistant; goal = find root cause; behavior = call diagnostics in a sane order, synthesize aloud, end with a ranked hypothesis; **constraint = read-only — propose remediation, never execute.**
2. Test in **text mode first** so you debug reasoning, not audio+reasoning at once.

**Watch:** keep the static block (role + tool schemas + constraints) stable at the front — clean prefix, and later the slot where retrieved memory pins (stretch X1).
**DONE WHEN:** typed investigation converges on incident #1's correct root cause.

### I4 ⛔ — Voice end-to-end + RCA push
**Actions:**
1. Run I3's agent over the voice pipeline (WebRTC).
2. On convergence, emit an RCA artifact (markdown: timeline + root cause + *proposed* remediation) and "push to phone" — simplest: write to a file/endpoint you open on your phone, or a webhook.
3. Confirm it *speaks synthesis*, not log lines. Route audio to laptop speakers so the room hears it.

**Watch:** don't over-build the push — a URL you refresh on your phone is enough.
**DONE WHEN:** by voice, incident #1 converges and an RCA appears on the phone. **CHECKPOINT 11:30 — no round-trip → grab a Pipecat/Daily mentor now.**

---

## Layer 2 — Smart on-call routing (the escalation act)

**Sequencing rule:** Layer 1 (triage) + the Cekura loop are the spine — they come first and must work. Layer 2 is the second act. The *routing brain* (R1/R2) is pure logic, text-testable, and can be built in Phase 1 once core triage converges; the *outbound call* (R3) depends on telephony being live (B3), so it lands in Phase 2/3. If outbound calling fights you, **fall back to announce-only** (R4) — the agent still makes and states the routing decision, you skip the live dial. 80% of the wow, near-zero risk.

### R1 ⛔(for this layer) — Engineer directory
**Actions:** in `mock_backend.py`, add an `ENGINEERS` dict next to the incident fixtures: per engineer `name`, `phone` (E.164), `location`, `timezone` (IANA, e.g. `Europe/London`), `working_hours` (local start/end), `teams` (e.g. `["database","payments"]`). Author 4–6 engineers spread across timezones so at any demo clock some are on-shift and some asleep.
**Watch:** make the split *clean* — for your headline incident, the on-shift person and the right-expertise person should be the **same** engineer, and the obvious-but-wrong choice (asleep, or wrong team) should be clearly excluded.
**DONE WHEN:** directory authored; covers ≥3 timezones and the teams your incidents touch.

### R2 ⛔(for this layer) — `find_on_call_engineer` tool (the brain)
**Actions:** add a direct-function tool that, for each engineer, computes current **local time** via stdlib `zoneinfo` (no deps), filters to those inside `working_hours`, then ranks the survivors by **team match** to the incident's affected service. Returns the best engineer + one backup, with a one-line reason ("Priya — London, 10:14 local, owns database"). Add a `DEMO_NOW` env override so the "current time" is deterministic on stage.
**Watch:** the tool's **docstring is its spec** (direct-function pattern) — state plainly: filter by working hours first, then expertise. Test in **text mode**: "DB incident at demo-time 03:00 PT → returns Priya, not Diego."
**DONE WHEN:** given an incident area + demo clock, it returns the correct on-shift, right-expertise engineer in text mode.

### R3 ⛔(for this layer) — `call_engineer` outbound call
**Actions:** add a tool that places an **outbound Twilio call** to the chosen engineer's number and bridges them to the bot to hear the briefing (incident + RCA + proposed remediation). Reuse the **same** `wss://api.pipecat.daily.co/ws/twilio` path as inbound — Twilio just *originates* the call (REST `calls.create` with `<Connect><Stream>` TwiML) instead of receiving it. *(verify Pipecat Cloud dial-out API with the Pipecat mentors on the day.)*
**Watch:** depends on B3 telephony being deployed and working. Call **one controlled demo phone you own** — not a random number — so the beat is reliable and consent is a non-issue. Outbound originates *from* your Twilio number; verified-caller-ID rules on trial accounts can bite.
**DONE WHEN:** the agent autonomously dials your demo phone and briefs the "engineer" by voice.

### R4 — Announce-only fallback (build this first if R3 is shaky)
**Actions:** the agent states the routing decision aloud ("Paging Priya — London, on-shift, owns the database") and optionally fires an SMS/webhook notification, without a live voice call.
**DONE WHEN:** routing decision is spoken + visible; demo works even with outbound calling disabled.

**Demo beat:** "3 AM in SF. Agent diagnoses a payments-DB pool exhaustion. Diego (SF, frontend) is asleep; Priya (London, mid-morning, owns the DB) is on-shift — **agent pages Priya.** Her phone's ringing." → live brief.

---

## Lunch (12:00–12:30) — working

---

## Phase 2 — Breadth + telephony (12:30–2:00)

### B1 — Incidents #2–4
**Actions:** clone the I1 schema for genuinely different root causes: DB pool exhaustion (distinct trigger), cert expiry, downstream-service timeout. Optional 5th: routing/BGP flap. Each gets ground truth.
**Watch:** make root causes *really* different so the eval tests discrimination, not four reskins.
**DONE WHEN:** 3–4 incidents, each with ground truth.

### B2 — Verify convergence per incident
**Actions:** run each by voice; confirm correct root cause. Flaky one → mark "not for live demo" rather than fix now.
**DONE WHEN:** each runs clean at least once.

### B3 ⛔ — Deploy to Pipecat Cloud + Twilio dial-in
**Actions:**
1. Review `pcc-deploy.toml`. Upload secrets: `pc cloud secrets set flower-bot-secrets --file .env` (rename the bot/secret set if you like). Then `pc cloud deploy`.
2. `pc cloud organizations list` → get `YOUR_ORG_NAME`.
3. Create a **TwiML Bin** (Twilio console):
   ```xml
   <Response><Connect>
     <Stream url="wss://api.pipecat.daily.co/ws/twilio">
       <Parameter name="_pipecatCloudServiceHost" value="flower-bot.YOUR_ORG_NAME"/>
     </Stream>
   </Connect></Response>
   ```
4. Attach the TwiML Bin to your number's **Voice Configuration**. Call the number; confirm investigation over the phone. (Twilio **Dev Phone** is handy for test calls.)

**Watch:** the `wss://` endpoint is Pipecat Cloud's — already TLS. Failures here are now isolated to **Twilio config** (TwiML Bin attached? right `_pipecatCloudServiceHost`? verified caller ID on trial?) or a deploy that didn't pick up secrets. **>45 min fighting → CUT to the local WebRTC / Dev-Phone demo.**
**DONE WHEN:** a real phone call reaches the deployed agent and it investigates.

---

## Phase 3 — Cekura baseline (2:00–3:00)

### C1 ⛔ — Wire Cekura via Claude Code
**Actions:**
1. In Claude Code: `/plugin marketplace add cekura-ai/cekura-skills` → `/plugin install cekura@cekura-skills` → `/setup-mcp` (wires the MCP + your Cekura API key). *(verify)*
2. Connect your agent — **provider = `Pipecat`**, supply your **Pipecat Cloud API key** + **agent name** (`flower-bot` from `pcc-deploy.toml`); optional Assistant ID / Agent-Config JSON / Room-Properties JSON. Note your **`agent_id`** (every later command needs it).
3. Run `/cekura-report` once to confirm Cekura connects and returns a transcript.

**Watch:** Cekura tests the **deployed Pipecat Cloud agent over WebRTC via the Pipecat Cloud API — NOT your Twilio number.** So (a) `pc cloud deploy` must be done first (Phase 2 B3), and (b) the entire Cekura centerpiece survives even if Twilio is flaky. This is the likeliest Phase-3 time sink — if it stalls, grab the on-site Cekura team. Command surface: `/create-metric` `/autogen-eval` `/run-evals` `/eval-results` `/evaluate-calls` `/improve-metric` `/cekura-report`.
**DONE WHEN:** `/cekura-report` connects and returns at least one scored transcript.

### C2 ⛔ — ~6 scenarios
**Actions:** scenarios spanning the incidents (2 deploy-related, 1 cert, 1 timeout, 1 ambiguous-symptom, 1 mid-investigation correction). Encode the expected root cause as the success criterion. (`/cekura-report` can generate 10–20 evaluators — curate down to the ones that map to your fixtures.)
**Plus ≥2 routing scenarios (Layer 2):** encode the expected *paged engineer* as the success criterion — e.g. "DB incident at demo-time 03:00 PT → must page Priya (awake, DB), not Diego (asleep, frontend)"; and an expertise-vs-availability conflict to prove the logic isn't just timezone. This makes Cekura score **decision quality**, not just conversation.
**DONE WHEN:** ~6 incident scenarios + ≥2 routing scenarios saved with pass criteria. **→ Pre-authored, paste-ready: see `cekura-eval-plan.md` (9 metrics, 2 personas, 9 scenarios E1–E9 with exact expected root causes + expected pages).**

### C3 ⛔ — 2 personas
**Actions:** a calm engineer and a stressed/interruptive one who redirects mid-investigation ("no — the canary, not the fleet"). The interruptive persona doubles as your live "interrupt it" demo beat.
**DONE WHEN:** both personas configured.

### C4 ⛔ — Baseline run (+ the GPT-vs-Nemotron A/B)
**Actions:**
1. Run the suite against the **Nemotron** bot; record the baseline pass rate.
2. **Bonus / headline:** run the *same* suite against the **GPT-4.1** bot and capture the side-by-side (pass rate + latency). This is the README's suggested comparison and hits both judging criteria.
**Watch:** you *want* some Nemotron failures — a 100% baseline means scenarios are too easy and the loop has nothing to show.
**DONE WHEN:** a baseline pass-rate number exists (ideally Nemotron vs GPT side-by-side).

---

# 🧊 FREEZE — 3:30 PM. No core product edits past here.

---

## Phase 4 — Loop + latency (3:30–5:00) · parallel lanes (solo → Lane A first)

### Lane A — Reliability (the curve)

**A1 — Seed the unfixable-by-prompt failure**
**Actions:** introduce one failure that's a *CodeBug*, not wording — e.g. a tool returns empty on a malformed arg, or history truncated before the deploy timeline. Confirm it fails baseline for a code reason.
**DONE WHEN:** the scenario fails baseline due to code, not prompt.

**A2 ⛔ — Run the self-improving loop** (`cekura-self-improving-agent` skill / `/evaluate-calls` → improve → redeploy)
**Actions:** give the loop your `agent_id`, the eval/scenario IDs, and the **redeploy command = `pc cloud deploy`** up front. Let it run ≥2 iterations applying fixes and re-testing. It reruns failing tests 3–4× to separate real bugs from flakes.
**Watch:** Cekura tests the **deployed** agent, so the redeploy command *must* be `pc cloud deploy` — a local restart won't be seen. Each iteration includes a cloud build (minutes), so plan for **2–3 solid iterations, not 20**. The planted **CodeBug** (A1) is what fails every rerun and the loop can't prompt-fix → that's your escalation beat.
**DONE WHEN:** loop runs ≥2 iterations, applies fixes, and the CodeBug visibly resists prompt-only fixing.

**A3 ⛔ — Capture the artifacts**
**Actions:** screenshot/record (a) pass-rate climbing across iterations, (b) the full-set **regression sweep** catching a silently-broken scenario (the dip), (c) the **escalation / no-change signal** that drives the CodeBug to a code-level guard.
**Watch:** confirm the escalation signal is *observable on screen*.
**DONE WHEN:** curve + regression catch + escalation beat captured.

### Lane B — Latency

**L1 ⛔ — GPT-4.1 vs Nemotron, measured**
**Actions:** from C4's A/B, pull V2V latency for both models on the same scenarios. (Prefix-cache toggling is *not* available — the model is hosted — so this comparison is the latency headline instead.)
**DONE WHEN:** before/after-style table: GPT vs Nemotron latency (and quality) recorded.

**L2 — First-chunk TTS streaming**
**Actions:** confirm Gradium TTS streams first audio on sentence boundary (Pipecat chunks on sentences); measure TTFB-to-first-audio. The *perceived* latency win.
**DONE WHEN:** first-audio latency recorded.

**L3 ⛔ — Waterfall slide**
**Actions:** one slide: ASR → turn-detect → LLM TTFT → TTS first-chunk → network, real numbers, GPT-vs-Nemotron + TTS-streaming annotated; one headline V2V number (measured on the **deployed** path).
**Watch:** static slide beats live numbers under pressure.
**DONE WHEN:** slide done with real numbers.

### D1 🔀 — Trace view (whoever's free)
**Actions:** Pipecat dev UI if S4=Yes. Else a minimal append-only page tailing structured events: `ALERT → tool-call → tool-result → hypothesis(confidence) → RCA pushed`. No animations.
**Watch:** legible-slightly-behind beats pretty-janky. Don't narrate it live — let it corroborate the voice.
**DONE WHEN:** events render legibly, roughly synced to the voice.

---

## Phase 5 — Lock / record / submit (5:00–6:00)

**F1 ⛔ — Lock the run**
**Actions:** pick the 3–4 incidents that ran clean twice; fix demo order; write the literal click/dial/say sequence.
**DONE WHEN:** run order fixed; clean twice.

**F2 ⛔ — Record backup BY 5:15**
**Actions:** screen-record the trace view + Cekura curve with call audio mixed in. Stage-failure insurance, more legible than live.
**DONE WHEN:** clean recording saved locally.

**F3 ⛔ — Submission**
**Actions:** write it around the artifacts — (1) live triage on **Nemotron**, (2) **autonomous outbound routing** (follow-the-sun paging to the right awake engineer), (3) Cekura pass-rate curve + regression/escalation *including routing-decision scoring*, (4) latency table (GPT vs Nemotron). Lead with the problem ("paged, no laptop"); name the CodeBug-escalation and the follow-the-sun outbound page as the two unique twists; call out NVIDIA OSS model + Cekura loop (the two things judges asked for).
**DONE WHEN:** drafted.

**F4 — Rehearse twice**
**Actions:** full 5-min run incl. the interrupt beat and the pivot line.
**DONE WHEN:** two clean rehearsals.

**F5 ⛔ — Submit by 5:45**
**DONE WHEN:** submission confirmed (15-min buffer).

---

## Stretch (post-freeze, only if F1–F3 done + backup recorded)

**X1 — Memory layer:** store past incidents (symptom → root cause → resolution); your other fixtures *are* the store. On a new incident, embed live symptoms (any small embedding model), cosine-sim over ~12 vectors in memory (no vector DB), inject top-1 as **text into the pinned prefix slot** at the front of the system prompt. Run Cekura with memory off vs on; capture the accuracy + turns-to-resolution lift. *Retrieve text, never KV.*

**X2 — Concurrency clip:** run 2–3 Cekura sessions at once; record latency holding under load. Only if it's a clean clip.

**X3 — Model-swap clip:** one demo line — "same agent, swap `bot-nemotron.py` ↔ `bot-gpt.py`, Cekura re-scores in minutes." Reinforces the eval-driven story.

---

## Reminders that override everything

- The **⛔ chain** is the demo. Pull mentors (Pipecat/Daily, Cekura, Twilio, NVIDIA — all on-site) onto at-risk ⛔ tasks first.
- **Local WebRTC → Pipecat Cloud → Twilio, in that order.** The public `wss://` is Pipecat Cloud's; don't build TLS yourself.
- **Judges asked for two things:** great Cekura usage to improve the agent, and NVIDIA OSS models. Nemotron primary + the Cekura loop + GPT-vs-Nemotron A/B nails both.
- **Freeze 3:30, record 5:15, submit 5:45.** Non-negotiable.
- Solo? Phase 4 lanes go sequential → finish **Lane A (the curve)** first; latency collapses to the GPT-vs-Nemotron table.
- **Layer 2 (routing) is the second act, not the spine.** Triage + Cekura loop work *first*. The routing brain (R1/R2) is cheap and text-testable — build it early; the outbound call (R3) is the risky part — if it's not solid by freeze, ship **announce-only (R4)** and don't look back.
- The self-hosting risk is gone — don't reintroduce it. If a hosted endpoint is down, switch bots (`bot-gpt.py`) and keep moving; revisit nothing.
