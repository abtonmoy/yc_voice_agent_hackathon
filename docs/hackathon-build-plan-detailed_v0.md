# Voice Incident-Triage Agent — Detailed Action Plan

Companion to the task breakdown. Every task now has: **Actions** (what to do, step by step), **Watch** (the gotcha most likely to bite), and **DONE WHEN** (the gate). Commands marked *(verify)* are best-known but versions move — confirm on the day.

**Golden rule:** if a step isn't on the critical path (⛔) and it's fighting you, skip it and move on. Working-and-narrow beats broad-and-broken.

---

## Standing principle — two endpoints, one of them public

This rule governs S6, B3, and C1 — internalize it before the day:

- **The model endpoint (vLLM/NIM) stays PRIVATE.** Twilio and Cekura never touch it. Only your Pipecat bot reaches vLLM, internally (localhost/VPC). Exposing raw inference to the internet is pure downside — free compute for strangers, zero benefit.
- **Only the Pipecat bot's transport endpoint goes PUBLIC**, because Twilio (phone) and Cekura (test calls) connect to it from outside.
- **That public endpoint must be `wss://` (secure WebSocket), not `ws://` and not plain HTTP.** Telephony media streams require TLS-terminated secure WebSockets — plain `ws://` is rejected. This single fact is the most common silent failure at integration time.
- **How you get a public `wss://` depends on where the bot runs:**
  - Bot on a **cloud instance** → use the platform's native URL (Modal web endpoint) or open the port + put TLS in front (AWS: security-group rule + cert/LB).
  - Bot on **your laptop** (behind NAT) → a **tunnel** (Cloudflare Tunnel or ngrok) gives you a public `wss://` URL forwarding to `localhost`, with TLS handled for you. For a hackathon this is the normal, expected answer.
- **Measure your headline latency on the real deploy path, not over a tunnel** — tunnels route through a provider POP and add latency that isn't your engineering.

---

## Phase 0 — Setup (8:00–9:00) · de-risk only

### S1 — Accounts + credits
**Actions:**
1. Create/log into: Cekura (`dashboard.cekura.ai`), Modal (or your AWS GPU console), Twilio, NVIDIA build (`build.nvidia.com`).
2. Grab API keys into one `.env` scratch file: `CEKURA_API_KEY`, Twilio SID/token + a phone number, NVIDIA/NIM key, Modal token.
3. Confirm GPU access actually launches an instance (don't assume the credit works).

**Watch:** Twilio number provisioning and trial-account restrictions (verified caller IDs) can surprise you — provision the number now.
**DONE WHEN:** logged into all four; a GPU instance has started at least once; keys in `.env`.

### S2 — Clone the reference repo
**Actions:**
1. `git clone https://github.com/pipecat-ai/nemotron-january-2026 && cd nemotron-january-2026`
2. Read the README end-to-end once. Note: it ships a Nemotron Speech ASR WebSocket server (`src/nemotron_speech/server.py`), a Pipecat STT client (`pipecat_bots/nvidia_stt.py`), a Magpie TTS WebSocket server, and Dockerfiles for DGX Spark / RTX 5090 / Modal.
3. Install deps per README (`uv sync` or the Modal path).

**Watch:** the repo is built around *local single-GPU* inference (llama.cpp interleaving). You will replace the LLM half with vLLM and keep the ASR + TTS servers. Don't get pulled into the llama.cpp interleaving code — you're deleting that path.
**DONE WHEN:** repo installed; you can identify the ASR server, TTS server, and the Pipecat bot entrypoint.

### S3 ⛔ — vLLM serves Nemotron 3 Nano
**Actions:**
1. `vllm serve nvidia/nemotron-3-nano-30b-a3b` *(verify exact HF id + flags)* with reasoning **off** and `--enable-prefix-caching`.
2. Pick the quant that fits your GPU: BF16 (~72GB), Q8 (~32GB), Q4 (~24GB).
3. Test: `curl http://localhost:8000/v1/chat/completions` with a trivial message.

**Watch:** this is the **single biggest risk** — Nemotron 3 Nano is a hybrid Mamba-Transformer MoE; hybrid arch support in vLLM has historically been patchy and the Daily team needed a small patch for the *reasoning* output format. Running non-reasoning sidesteps that patch. If it won't load, do **not** sink an hour — go to the NIM fallback (gate at 9:00).
**DONE WHEN:** `curl` returns a coherent completion.

### S4 ⛔ — Prefix-cache reality check
**Actions:**
1. Send a request with a long shared prefix (fake system prompt + tool schemas), note TTFT.
2. Send a second request with the **same** prefix, different tail.
3. Read vLLM's prefix-cache hit-rate metric (logs or `/metrics`) and compare TTFT.

**Watch:** on a Mamba-hybrid model the block-based prefix cache may quietly no-op (Mamba layers carry recurrent *state*, not attention KV). **Record the result either way** — it decides whether your latency story leans on prefix caching (if it engages) or on TTS-streaming (if it doesn't). Don't build the slide narrative until you know.
**DONE WHEN:** turn-2 TTFT/hit-rate measured and written down.

### S5 🔀 — Trace-view source decision
**Actions:**
1. Launch the Pipecat Playground / Whisker debugger against the stock bot.
2. Trigger a tool call; check whether tool calls + their results render legibly.

**Watch:** "legible from the back of a room" is the bar, not "technically visible."
**DONE WHEN:** Yes/No recorded. Yes → D1 is free. No → D1 = build a simple log tailer (planned in Phase 5).

### S6 ⛔ — Public path works (the reachability check) — *do this NOW, not at B3*
**Actions:**
1. Decide where the bot runs → tunnel (laptop) or platform URL (cloud). See the Standing Principle above.
2. Stand up the public `wss://` address: `cloudflared tunnel` / `ngrok http <port>` for laptop, or open-port + TLS for cloud. *(verify current tunnel flags)*
3. **Echo smoke test:** point a trivial echo WebSocket at the public URL; confirm an external client connects over **`wss://`** and frames round-trip — *before* any Twilio/Cekura wiring.
4. Note the stable public URL; if free-tier ngrok rotates the URL on restart, write down that every restart = re-point Twilio.

**Watch:** the killer is `ws://` vs `wss://` — telephony rejects insecure. Prove TLS works here, in isolation, so that when B3/C1 break you *know* the bug is in Twilio/Cekura config, not your exposure. Keep vLLM **off** this public path — model stays private.
**DONE WHEN:** an external client reaches your public `wss://` endpoint and frames echo back.

**🔀 GATE @ 9:00:** S3 clean → vLLM. S3 fails → swap LLM to **NIM hosted Nemotron** (OpenAI-compatible; Pipecat has a NIM service). Pipeline stays alive; revisit vLLM only as a post-freeze optimization.

### V1 ⛔ — Pipecat → vLLM
**Actions:**
1. In the bot, replace the repo's local LLM service with Pipecat's `OpenAILLMService` (`base_url=http://<vllm-host>:8000/v1`, `api_key="x"`, `model=<nemotron id>`). *(verify import path for your Pipecat version)*
2. If using NIM instead, point `base_url` at the NIM endpoint with the NIM key.
3. Keep the repo's ASR + Magpie services untouched.

**Watch:** model-name string must match exactly what vLLM/NIM advertises, or you get 404s. Disable reasoning in the request params.
**DONE WHEN:** the LLM service returns tokens inside the running pipeline (text path).

### V2 ⛔ — First voice round-trip
**Actions:**
1. Run the bot with a WebRTC/web transport (the repo's default dev transport).
2. Open the client, speak a sentence, confirm: ASR transcribes → LLM responds → Magpie speaks back.
3. Route audio to laptop speakers (not just headphones) so the room will hear it later.

**Watch:** mic permissions, sample-rate mismatches between ASR server and transport, WebSocket URLs for ASR/TTS servers pointing at the right host.
**DONE WHEN:** you speak and hear a coherent spoken reply. **CHECKPOINT 10:30 — no round-trip → mentor now.**

---

## Phase 2 — Make it an incident agent (10:30–12:00)

### I1 ⛔ — Fixture schema (one incident)
**Actions:**
1. Define a JSON shape per incident: `alert` (what paged you), `deploys` (recent deploy timeline), `logs` (a few pre-correlated lines), `metrics` (key series snapshots), and **`ground_truth_root_cause`** (string + the deploy/metric that proves it).
2. Author incident #1 = "bad deploy exhausts DB connection pool."
3. Keep logs/metrics *small and already-correlated* — the tool returns a summary, not a 4KB dump.

**Watch:** the `ground_truth_root_cause` field is what makes Cekura scoring possible later — don't skip it. Make timestamps line up (deploy at 14:30 → errors at 14:32) so the agent has a real signal to find.
**DONE WHEN:** incident #1 JSON validates and contains ground truth.

### I2 ⛔ — Mock tools
**Actions:**
1. Implement `get_alerts()`, `get_deploy_history()`, `get_logs(service)`, `get_metrics(name)` as Pipecat function-calling tools that read the fixture.
2. Each returns a **synthesized summary string** ("200× connection-refused to payments-db starting 14:32, 2 min after deploy abc123"), not raw rows.
3. Register the tool schemas with the LLM service.

**Watch:** tool *descriptions* drive accuracy more than anything — make them unambiguous and state when to use each. This is the lever Cekura's loop will tune.
**DONE WHEN:** each tool callable and returns a pre-correlated summary.

### I3 ⛔ — Triage system prompt
**Actions:**
1. Write the prompt: role = on-call triage assistant; goal = find root cause; behavior = call diagnostics in a sane order, synthesize findings aloud, end with a ranked hypothesis; **constraint = read-only, propose remediation but never execute**.
2. Test in **text mode first** (type the incident symptoms) so you're not debugging audio + reasoning at once.

**Watch:** keep the static part of the prompt (role + tool schemas + constraints) as a stable block at the front — that's your cacheable prefix and, later, where retrieved memory pins.
**DONE WHEN:** typed investigation converges on incident #1's correct root cause.

### I4 ⛔ — Voice end-to-end + RCA push
**Actions:**
1. Run I3's agent over the voice pipeline.
2. On convergence, emit an RCA artifact (markdown: timeline + root cause + *proposed* remediation) and "push to phone" (simplest: write to a file/endpoint you open on the phone, or send via a webhook).
3. Confirm it speaks synthesis, not log lines.

**Watch:** don't over-build the push — a URL you refresh on your phone is enough for the demo.
**DONE WHEN:** by voice, incident #1 converges and an RCA appears on the phone.

---

## Lunch (12:00–12:30) — working

---

## Phase 3 — Breadth + telephony (12:30–2:00)

### B1 — Incidents #2–4
**Actions:** clone the I1 schema for: DB pool exhaustion (distinct trigger), cert expiry, downstream-service timeout. Optional 5th: a routing/BGP flap (plays to your Control Plane background). Each gets ground truth.
**Watch:** make root causes *genuinely different* so the eval suite tests real discrimination, not four reskins of one bug.
**DONE WHEN:** 3–4 incidents, each with ground truth.

### B2 — Verify convergence per incident
**Actions:** run each incident by voice; confirm correct root cause.
**Watch:** if one incident is flaky, mark it "not for live demo" rather than fixing it now.
**DONE WHEN:** each runs clean at least once.

### B3 🔀 — Twilio dial-in
**Actions:**
1. Add the Twilio transport/serializer to the Pipecat pipeline (Pipecat ships a Twilio serializer for Media Streams). *(verify current config)*
2. Configure **two public URLs**: the number's voice webhook → a public TwiML endpoint that returns `<Connect><Stream url="wss://<your-S6-url>/...">`. Both reference the `wss://` path you already proved in **S6**.
3. Add Twilio signature validation on the webhook (cheap, and it's a real public endpoint).
4. Co-locate the bot in the same region as Twilio media servers to limit added latency.
5. Call the number; confirm investigation over the phone.

**Watch:** because S6 already proved the public `wss://` path, any failure here is now isolated to **Twilio config** (webhook URL, `<Stream>` vs `ws://`, verified caller ID on trial) — not your exposure. **>45 min fighting → CUT to phone-browser demo.**
**DONE WHEN:** a phone call reaches the agent and it investigates.

---

## Phase 4 — Cekura baseline (2:00–3:00)

### C1 ⛔ — Connect agent to Cekura
**Actions:** register the agent in the Cekura dashboard / via API so Cekura can call it (phone number or API endpoint). Note your `agent_id`. Then place **one** Cekura test call and confirm a transcript comes back.
**Watch:** Cekura may reach the agent via a **different path** than Twilio (API/SIP vs the phone number) — so reachability is its *own* smoke test, resting on the same public `wss://` from S6 but not the same as B3. This integration is the likeliest Phase-4 time sink — have Lane B glance at it ~2:30 if it's not moving.
**DONE WHEN:** Cekura places one test call that connects and returns a transcript.

### C2 ⛔ — ~6 scenarios
**Actions:** create scenarios spanning the incidents (e.g. 2 deploy-related, 1 cert, 1 timeout, 1 ambiguous-symptom, 1 mid-investigation correction). Encode the expected root cause as the success criterion.
**DONE WHEN:** 6 scenarios saved with pass criteria.

### C3 ⛔ — 2 personas
**Actions:** configure a calm engineer and a stressed/interruptive engineer who redirects mid-investigation ("no — the canary, not the fleet").
**Watch:** the interruptive persona doubles as your live "interrupt it" beat — make it realistic.
**DONE WHEN:** both personas configured.

### C4 ⛔ — Baseline run
**Actions:** run the suite (dashboard, or `POST https://api.cekura.ai/test_framework/v1/scenarios/run` with `agent_id` + `scenario_ids`, header `X-CEKURA-API-KEY`). Record the baseline pass rate.
**Watch:** you *want* some failures here — a 100% baseline means your scenarios are too easy and the loop has nothing to show.
**DONE WHEN:** a baseline pass-rate number exists.

---

# 🧊 FREEZE — 3:00 PM. No core product edits past here.

---

## Phase 5 — Loop + latency (3:00–4:45) · parallel lanes

### Lane A — Reliability

**A1 — Seed the unfixable-by-prompt failure**
**Actions:** introduce one failure that's a *CodeBug*, not wording — e.g. a tool that returns empty on a malformed arg, or history truncated before the deploy timeline. Confirm it fails on baseline for a code reason.
**Watch:** it must fail *reliably*, or the escalation beat won't fire.
**DONE WHEN:** the scenario fails baseline due to code, not prompt.

**A2 ⛔ — Run the self-improving loop**
**Actions:**
1. `claude plugin marketplace add cekura-ai/cekura-skills` (MCP-enabled editor). *(verify)*
2. Invoke: `improve my agent <agent_id> using scenarios <ids>, redeploy command: <your restart cmd>`.
3. Provide the redeploy command up front (Setup gate of their skill) so edits actually reach the running agent.
**Watch:** without a working redeploy command, the loop edits source the live process never re-reads — wasted iterations.
**DONE WHEN:** loop runs ≥2 iterations and applies fixes.

**A3 ⛔ — Capture the artifacts**
**Actions:** screenshot/record (a) the pass-rate climbing across iterations, (b) the **full-set regression sweep** catching a silently-broken scenario (the dip), (c) the **escalation / no-change signal** that drives the CodeBug to a code-level guard.
**Watch:** confirm the escalation signal is *observable on screen* — you flagged this; if it's buried, the beat loses punch.
**DONE WHEN:** curve + regression catch + escalation beat captured.

### Lane B — Latency

**L1 ⛔ — Prefix caching before/after**
**Actions:** with S4's result in hand, capture TTFT with caching off vs on across a multi-turn investigation. If S4 showed it no-ops on the hybrid model, pivot the headline to L2.
**DONE WHEN:** before/after TTFT recorded.

**L2 — First-chunk TTS streaming**
**Actions:** confirm Magpie streams first audio on sentence boundary (repo already chunks on sentences); measure TTFB-to-first-audio. This is the *perceived* latency win.
**DONE WHEN:** first-audio latency recorded.

**L3 ⛔ — Waterfall slide**
**Actions:** one slide: ASR → turn-detect → LLM TTFT → TTS first-chunk → network, with real numbers and the caching + streaming wins annotated; one headline V2V number.
**Watch:** static slide is fine and clearer than live numbers under pressure.
**DONE WHEN:** slide done with real numbers.

### D1 🔀 — Trace view (whoever's free)
**Actions:** Playground if S5=Yes. Else a minimal append-only web page tailing your agent's structured events: `ALERT → tool-call → tool-result → hypothesis(confidence) → RCA pushed`. No animations.
**Watch:** keep updates simple; legible-slightly-behind beats pretty-janky. Don't narrate it live — let it corroborate the voice.
**DONE WHEN:** events render legibly, roughly synced to the voice.

---

## Phase 6 — Lock / record / submit (4:45–6:00)

**F1 ⛔ — Lock the run**
**Actions:** pick the 3–4 incidents that ran clean twice; fix the demo order; write the literal click/dial/say sequence.
**DONE WHEN:** run order fixed; clean twice.

**F2 ⛔ — Record backup BY 5:15**
**Actions:** screen-record the trace view + Cekura curve with call audio mixed in. This is your stage-failure insurance and is *more* legible than live.
**DONE WHEN:** clean recording saved locally.

**F3 ⛔ — Submission**
**Actions:** write it around the three artifacts (live triage, pass-rate curve, latency table). Lead with the problem ("paged, no laptop"), name the two optimizations, name the CodeBug-escalation as the unique twist.
**DONE WHEN:** drafted.

**F4 — Rehearse twice**
**Actions:** full 5-min run, including the interrupt beat and the pivot line.
**DONE WHEN:** two clean rehearsals.

**F5 ⛔ — Submit by 5:45**
**Actions:** submit with a 15-min buffer. Don't ride the 6:00 deadline.
**DONE WHEN:** submission confirmed.

---

## Stretch (post-freeze, only if F1–F3 done + backup recorded)

**X1 — Memory layer**
**Actions:** store past incidents (symptom summary → root cause → resolution) — your other fixtures *are* the store. On a new incident: embed live symptoms (any small embedding model), cosine-similarity over ~12 vectors in memory (no vector DB needed), inject the top-1 as **text into the pinned prefix slot** (front of the system prompt). Run Cekura with memory off vs on; capture the accuracy + turns-to-resolution lift.
**Watch:** retrieve **text**, never KV. The pinned slot stays identical across turns within a call, so prefix caching reuses its KV for free and correctly. Don't splice stored KV.
**DONE WHEN:** off/on Cekura comparison shows a lift.

**X2 — Concurrency clip:** run 2–3 Cekura sessions at once; record latency holding under load (continuous batching). Only if it's a clean clip.

**X3 — Selective reasoning:** reasoning OFF for conversational turns, ON only for the final hypothesis step. One config change + one demo line.

---

## Reminders that override everything

- The **⛔ chain** is the demo. Protect it; pull people/mentors onto at-risk ⛔ tasks before anything else.
- **Prove the public `wss://` path (S6) in the setup hour.** Model private, bot public, TLS required. Front-loading this is what makes B3/C1 failures *findable in seconds* instead of bisected at 4 PM.
- **Freeze 3:00, record 5:15, submit 5:45.** These three times are non-negotiable.
- Solo? Phase 5 lanes go sequential → finish **Lane A (the curve)** first; latency collapses to "prefix-cache TTFT before/after, one number."
- Keep the cut list cut. Q&A-ready honest answer on semantic-KV reuse: "research-grade, correctness hazard — roadmap, not build."
