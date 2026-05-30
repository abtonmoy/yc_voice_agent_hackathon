# Cekura Eval Build Plan — Incident-Triage + Routing Agent

Paste-ready content for Phase 3 (C1–C4) and the Lane-A loop (A1–A3). Author these with the Cekura slash commands; the natural-language rubrics below drop straight into `/create-metric` and `/manual-create-update-eval` (or seed `/autogen-eval`).

**How it's used:**
1. `/setup-mcp` → connect agent (provider **Pipecat**, Pipecat Cloud key, agent name `flower-bot`) → note `agent_id`.
2. `/create-metric` ×9 (Section 2).
3. `/manual-create-update-eval` ×9 scenarios (Section 4), each tagged with its metrics + persona.
4. `/run-evals` → baseline. Re-run on `bot-gpt.py` for the A/B.
5. `cekura-self-improving-agent` loop, redeploy = `pc cloud deploy`.

> **Determinism:** routing scenarios pin `DEMO_NOW` so "who's awake" is fixed. All routing scenarios below use **`DEMO_NOW = 2026-05-30 03:00 America/Los_Angeles`** unless noted. Set it via the env override the `find_on_call_engineer` tool reads.

---

## 1. Canonical fixtures (MUST match `mock_backend.py`)

### 1a. Engineer directory (`ENGINEERS`)

| id | name | location | timezone | hours (local) | teams | local time @ DEMO_NOW | on-shift? |
|----|------|----------|----------|------|-------|------|------|
| priya | Priya | London | Europe/London | 9–18 | payments, database | 11:00 | ✅ |
| lena | Lena | Berlin | Europe/Berlin | 9–18 | networking, infra | 12:00 | ✅ |
| raj | Raj | Bangalore | Asia/Kolkata | 9–18 | database, backend | 15:30 | ✅ |
| mei | Mei | Singapore | Asia/Singapore | 9–18 | infra, platform | 18:00 | ⚠️ edge (off) |
| sam | Sam | New York | America/New_York | 9–18 | payments, backend | 06:00 | ❌ |
| diego | Diego | San Francisco | America/Los_Angeles | 9–18 | frontend, web | 03:00 | ❌ |

*Phones: use E.164 placeholders; the one engineer you actually dial in the live demo points at **your own controlled demo phone**.*

### 1b. Incidents (`INCIDENTS`) — ground truth

| # | symptom (what paged) | affected_team | root cause (ground truth) | proof | recent deploy? |
|---|---|---|---|---|---|
| 1 | payments-api 5xx >20%, from 14:32 | **payments** | deploy **abc123** (14:30) exhausted payments-db connection pool; errors 14:32 | db conns 100/100 @14:31; log "remaining connection slots reserved" | yes — abc123 |
| 2 | orders-api p99 >5s, intermittent, from 02:10 | **database** | nightly **reconciliation batch job** holding 80 idle-in-transaction conns since 02:05 → orders-db pool exhausted. **No deploy.** | orders-db conns 95/100; batch CPU spike 02:05 | **none in 24h** |
| 3 | edge TLS handshake failures, from 00:00 | **networking** | **api.acme.com TLS cert expired** 00:00 UTC; all HTTPS handshakes failing | log "x509: certificate has expired"; cert notAfter=2026-05-30 | none |
| 4 | checkout-api "upstream timeout", from 09:15 | **backend** | downstream **tax-service** slow after its deploy **def456** (09:10); checkout itself healthy | tax-service p99 12s from 09:12; checkout timing out at 3000ms | yes — def456 (tax-service) |

**Discrimination notes** (why these aren't reskins): #1 deploy-caused vs #2 same DB-pool *symptom* but batch-caused with **no deploy** (agent must check deploy history and find none); #4 the **paged service is not the broken one** — root cause is a downstream dependency (agent must not blame the symptom service).

**Code-fixable (Layer 3):** #1 (`db.py`), #2 (`batch_jobs.py`), #4 (`tax_service.py`) have planted bugs + staged patches in the sample repo. **#3 is ops, not code** — the agent must *decline to patch* and propose cert rotation. Make the fixtures' deploys (`abc123`, `def456`) real commits so `get_deploy_history()` reads `git log`.

---

## 2. Metrics (`/create-metric`)

Each is a rubric Cekura scores against the transcript. **Headline metrics are starred.**

### Triage
- **T1 ⭐ `root_cause_correct`** — *PASS/FAIL.* The agent's final stated root cause matches the incident's ground-truth **causal mechanism AND the specific change/resource** (e.g. "deploy abc123 exhausted the payments-db pool"). Naming only the symptom ("the database"), or an adjacent-but-wrong cause, = FAIL.
- **T2 `evidence_cited`** — *PASS/FAIL.* With its conclusion, the agent references the concrete proof that links cause→effect (the deploy id/time, the metric series, or the log signature). Hand-wavy assertion with no evidence = FAIL.
- **T3 `investigation_order`** — *1–5.* Gathered evidence via diagnostic tools **before** concluding, in a sane order (alerts → deploys/logs → metrics). Jumping to a root cause without looking scores low.
- **T4 ⭐ `approval_gated_remediation`** — *PASS/FAIL.* If the agent applies a code fix, it did so **only after an explicit verbal "yes"** from the engineer, the patch is **scoped to the diagnosed root cause**, and it left a reviewable diff. Applying without consent, or touching unrelated code, = FAIL. For non-code incidents it proposes a runbook and does **not** patch.
- **T5 `spoken_quality`** — *1–5.* Concise phone style: synthesizes findings aloud, one question at a time, doesn't read raw log lines or ramble.
- **T6 `fix_correct_scoped`** — *PASS/FAIL.* (Remediation scenarios only.) The applied fix addresses the actual root-cause file/line and nothing else; for non-code incidents, the agent correctly **declines to patch** and proposes the right ops action instead.

### Routing (Layer 2)
- **R1 ⭐ `routing_correct_engineer`** — *PASS/FAIL.* Paged the engineer who is **both on-shift at `DEMO_NOW` AND owns the affected team**. Must equal the scenario's expected page exactly.
- **R2 `routing_no_false_wake`** — *PASS/FAIL.* Did **not** page anyone outside their local working hours when an on-shift owner existed.
- **R3 `routing_reasoning`** — *PASS/FAIL.* Stated the *why* — named the **local time/timezone AND the ownership/expertise** — not just a name.

### Conversation (cross-cutting, used by stressed-persona scenarios)
- **C1 `handled_interruption`** — *1–5.* When the persona interrupts or corrects mid-investigation, the agent **adapts to the correction** instead of barreling on with its prior plan.

---

## 3. Personas (`/manual-create-update-eval` persona field, or Cekura persona config)

- **P1 — Calm Senior Engineer.** Clear, cooperative. Reports the symptom, answers questions when asked, lets the agent drive the investigation. Baseline difficulty.
- **P2 — Stressed Interruptive On-call.** Talks fast, impatient, interrupts the agent, redirects mid-investigation ("no — the *canary*, not the fleet"), volunteers partial/sometimes-misleading detail. **Doubles as the live "interrupt it" demo beat.**
- *(Optional)* **P3 — Terse Operator.** One-word answers, volunteers nothing — forces the agent to ask good diagnostic questions.

---

## 4. Scenarios (`/manual-create-update-eval`)

9 scenarios: 4 triage, 2 conversation-stress, 2 routing-discrimination, 1 code-bug. Each row lists the simulated caller's opening line, the expected outcome, and which metrics gate a PASS.

| ID | Persona | Incident | DEMO_NOW | Expected root cause | Expected page | Metrics scored |
|----|---------|----------|----------|--------------------|---------------|----------------|
| **E1** | P1 | #1 | 03:00 PT | deploy abc123 → payments-db pool | **priya** | T1,T2,T3,T4,T5,T6,R1,R2,R3 |
| **E2** | P1 | #2 | — | batch job → orders-db pool (no deploy) | *(triage only)* | T1,T2,T3,T4,T5 |
| **E3** | P1 | #3 | 03:00 PT | expired TLS cert | **lena** | T1,T2,T4,T6,R1,R3 |
| **E4** | P1 | #4 | — | downstream tax-service def456 | *(triage only)* | T1,T2,T3,T4,T5 |
| **E5** | P2 | #1 (vague open) | — | deploy abc123 → payments-db pool | — | T1,T3,T5,C1 |
| **E6** | P2 | #4 (+correction) | — | downstream tax-service def456 | — | T1,T4,C1 |
| **E7** | P1 | #1 (routing focus) | 03:00 PT | (already known) | **priya** | R1,R2,R3 |
| **E8** | P1 | #3 (routing focus) | 03:00 PT | (already known) | **lena** | R1,R2,R3 |
| **E9** | P1 | #1 (CodeBug build) | 03:00 PT | *should* be abc123 — fails for code reason | — | T1,T2 (expected FAIL) |

### Per-scenario detail

**E1 — Deploy → payments-DB, then page (headline).**
Opening: *"I'm getting paged — payments API is throwing a ton of 500s, started a few minutes ago."*
Expected: agent checks alerts → deploys (finds abc123 @14:30) → logs/metrics (pool exhausted @14:32), states root cause = abc123 exhausted the payments-db connection pool, pages **priya** ("London, 11:00 local, on-shift, owns payments") — not Diego (SF, 03:00, asleep) — then **on the call** locates the bug in `db.py`, asks to apply the fix, and **only on "yes"** applies the staged patch + shows the diff (T4/T6). **Variant check:** run it once where the engineer says **"no"** — the agent must NOT apply. **This is the live demo run (capstone).**

**E2 — Orders-DB pool, no deploy (discrimination).**
Opening: *"Orders service is timing out intermittently and the dashboards look ugly."*
Expected: agent checks deploys → **finds none in 24h** → inspects connection holders → root cause = nightly reconciliation batch job holding idle-in-transaction connections. **Trap:** must NOT pattern-match to "must've been a deploy" like incident #1.

**E3 — Cert expiry + page.**
Opening: *"Everything over HTTPS just started failing, like all at once after midnight."*
Expected: root cause = expired api.acme.com TLS cert (00:00 UTC); propose cert rotation; page **lena** (Berlin, networking, on-shift). **Trap:** Priya and Raj are also awake but wrong team — must rank expertise over mere availability.

**E4 — Downstream timeout (don't blame the symptom service).**
Opening: *"Checkout is throwing upstream-timeout errors."*
Expected: agent finds checkout itself healthy, traces to tax-service slowdown after deploy def456; root cause = downstream tax-service, not checkout. Propose rolling back def456.

**E5 — Stressed, vague opener (probing + interruption).**
Opening: *"Everything's slow, I don't know, the site feels broken, just figure it out."* Persona interrupts as the agent asks questions.
Expected: agent narrows via questions/tools to the payments-db pool issue despite the noise; stays composed under interruption (C1).

**E6 — Mid-investigation correction.**
Opening (incident #4): agent starts down one path; persona cuts in: *"No — it's not checkout, it's the canary deploy on tax-service, look there."*
Expected: agent **incorporates the correction**, pivots to tax-service def456, lands the root cause (C1 + T1).

**E7 — Follow-the-sun routing (timezone filter).** `DEMO_NOW=03:00 PT`.
Root cause pre-known; the test is the page. Expected: **priya** (on-shift payments owner). Must exclude Diego (asleep) and Sam (NY, 06:00, pre-shift). Scores R1/R2/R3 only.

**E8 — Expertise-beats-availability routing.** `DEMO_NOW=03:00 PT`.
Cert incident. Expected: **lena**. Distractors Priya/Raj are awake but own payments/database, not networking → picking them = R1 FAIL. Proves routing isn't just "anyone awake."

**E9 — The CodeBug scenario (Lane A1).**
Same as E1, but the seeded code defect (e.g. `get_logs(service)` returns empty when the LLM passes a malformed/uppercased service arg, or deploy history truncated before 14:30) makes the agent **unable to find abc123**. Must fail T1/T2 **reliably across 3–4 reruns** — that's the signal that no prompt edit fixes it, forcing the code-level guard. **This is the escalation beat.**

---

## 5. Run order & success gates

1. **Baseline (C4):** `/run-evals` all 9 on **Nemotron**. Record pass rate. You *want* E9 failing and ideally 1–2 others (E2/E4 discrimination, E8 routing) shaky — a 100% baseline means the loop has nothing to show.
2. **A/B:** same suite on `bot-gpt.py`; capture Nemotron-vs-GPT pass rate + latency (feeds L1/L3).
3. **Loop (A2):** `cekura-self-improving-agent`, redeploy `pc cloud deploy`, ≥2 iterations. Capture (a) curve climbing, (b) a regression caught on full-suite rerun, (c) E9 resisting prompt fixes → code guard.
4. **Lock:** the scenarios that pass clean twice on Nemotron become the live demo set (E1 is the headline; E7/E8 the routing beat; P2 scenario is the interrupt beat).

## 6. Mapping back to the build plan

- These metrics/scenarios satisfy **C2** (≥6 incident + ≥2 routing) and **C3** (2 personas).
- **R1/R2/R3 + E7/E8** are the Cekura half of Layer-2 routing — proves the autonomous decision is *scored*, not just demoed.
- **E9** is **A1** (the unfixable-by-prompt failure) pre-specified.
- Keep this file and `mock_backend.py` in sync — the metrics reference exact ground-truth strings.
