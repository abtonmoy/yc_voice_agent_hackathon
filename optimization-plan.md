# Concentrated Triage Loop — Optimization Plan

Companion to `hackathon-build-plan-detailed.md` and `cekura-eval-plan.md`. Six client-side optimizations composed into **one coherent system** that runs on top of the hosted Nemotron stack — no model access, no server tuning required. Every win is measured against the E1–E9 eval suite, so the optimization story rides the same scoreboard as the agent story.

**The pitch in one line:** spend reasoning compute when it matters, predict it when it doesn't.

**Why "concentrated":** A/B/C/E/G/H are not six independent hacks. They share two state objects and a layered control flow. Removing any one degrades the others — that's the system claim.

---

## 0. ⚠️ Apply this AFTER the full system works end-to-end

**This is a post-MVP optimization layer, not a Phase 1 task.** Do not start any item in this plan until the agent already does the full thing on stock settings:

- ✅ Layer 1 triage converges on a root cause by voice (build-plan I4 done)
- ✅ Layer 2 routing decision works in text mode (R2 done), outbound or announce-only working (R3 or R4 done)
- ✅ Deployed to Pipecat Cloud, callable by phone (B3 done)
- ✅ Cekura wired and the **baseline** eval suite has run on the stock agent (C1–C4 done)

That last gate — **C4 baseline captured** — is the load-bearing one. The headline measurement (Section 8) is a *before/after table*; without a clean baseline number on the unmodified agent, you have no "before" column. **Run the baseline first, optimize second.**

**Why post-MVP, not parallel:**
- Every optimization here changes either the prompt prefix (B), the pipeline shape (C, E), the LLM service (A), or the conversation context (G, H). Applying them mid-build means debugging the optimization and the core agent simultaneously — you can't tell which layer broke.
- The eval scenarios (E1–E9) are tuned to the *stock agent's* failure modes. Applying optimizations before baseline means you don't know which scenarios were "naturally" hard vs. fixed-by-luck-during-construction.
- The judging story is "we measured, we optimized, we re-measured." That story requires a measured baseline to exist first.

**Slot in the build plan:** this work goes between **C4 (baseline run, ~3:00 PM)** and **🧊 freeze (3:30 PM)** if you have all the prereqs done by 3:00 — otherwise it goes into **Phase 4 Lane B (3:30–5:00)** as the latency work, with the freeze rule relaxed *only* for these six pieces (because they're additive and behind toggles).

**Toggle-gate every piece.** Each optimization gets an env flag (`OPT_A_ADAPTIVE_THINKING=true`, etc.). If anything misbehaves during the loop iterations or the live demo, flip it off and you're back to the working baseline. Never leave yourself without a kill switch.

---

## 1. Unifying principle — two scoreboards, one loop

Every optimization reads from or writes to one of two state objects. That's the concentration:

| Scoreboard | Lifetime | Contains | Read by | Written by |
|---|---|---|---|---|
| **`StaticAssets`** | deploy-time, immutable | pre-rendered TTS audio bytes, incident embeddings (numpy), stable system prompt string | C, B, H | nothing at runtime |
| **`InvestigationState`** | per-call, mutable | `confidence: float`, `turn_count: int`, `evidence_gathered: set[str]`, `incident_hypothesis: str \| None`, `prefetch_cache: dict[tuple, Any]` | A, E, G | LLM output parser, H, tool executor |

Without these, A/E/G/H are six unrelated tricks. With them, they form a loop.

---

## 2. Architecture (the data flow)

```
DEPLOY TIME — populate StaticAssets ──────────────────────────────────
  • (C) Render fixed phrases via Gradium once → bytes in memory
  • (B) Freeze system prompt: role + tool schemas only, no dynamic data
  • (H) Embed 4 incidents × 3 paraphrases → 12 numpy vectors

PER CALL — InvestigationState lives for the call ─────────────────────

   STT
    │
    ▼
   (H) Semantic Hint Injector
      cosine-sim user_text vs 12 vectors
      if max > 0.85: state.incident_hypothesis = matched
        emit as ToolResultFrame, NOT a prompt edit (B's prefix stays stable)
    │
    ▼
   (A) Thinking Gate
      decide enable_thinking for THIS turn:
        turn_count <= 2                              → OFF (warm-up)
        confidence > 0.85                            → OFF (we're sure)
        confidence < 0.5 AND turn_count >= 4         → ON  (we're stuck)
        about_to_route OR about_to_finalize          → ON  (high-stakes)
      set extra_body.chat_template_kwargs per request
    │
    ▼
   [LLM]
    │
    ▼
   Output Parser
      extract <conf>0.7</conf> tag (filtered from TTS stream)
      extract tool_calls
    │
    ▼
   (E) Speculative Prefetch
      if tool_call == get_alerts:
        bg: get_deploy_history(), get_logs(alerted_service)
      if incident_hypothesis set:
        bg: prefetch that incident's typical evidence path
      results → state.prefetch_cache
    │
    ▼
   Tool Executor (cache-aware)
      check state.prefetch_cache first → instant return
      else execute, update evidence_gathered
    │
    ▼
   (G) Termination Controller
      conf > 0.85 AND root_cause stated   → push CONCLUDE hint
      conf < 0.7 AND turns >= 4           → push DIG hint
        ("you haven't checked: {missing evidence categories}")
      hints are system-role messages appended to context, NOT prefix edits
    │
    ▼
   (C) TTS with Cache
      if response text matches a cached phrase → push audio directly
      else stream through Gradium
    │
    ▼
   Output
```

---

## 3. Shared state — concrete shape

Single source of truth. Put it in `triage_state.py`, instantiated once per call in `run_bot()`:

```python
from dataclasses import dataclass, field

@dataclass
class InvestigationState:
    confidence: float = 0.0
    turn_count: int = 0
    evidence_gathered: set[str] = field(default_factory=set)  # {"alerts","deploys","logs","metrics"}
    incident_hypothesis: str | None = None                    # e.g. "incident_1"
    prefetch_cache: dict[tuple, object] = field(default_factory=dict)
    root_cause_stated: bool = False

    def needs_thinking(self) -> bool:
        if self.turn_count <= 2: return False
        if self.confidence > 0.85: return False
        if self.confidence < 0.5 and self.turn_count >= 4: return True
        return False

    def should_terminate(self) -> bool:
        return self.confidence > 0.85 and self.root_cause_stated

    def missing_evidence(self) -> set[str]:
        return {"alerts", "deploys", "logs", "metrics"} - self.evidence_gathered
```

---

## 4. The six pieces — each as a build task

Same `Actions` / `Watch` / `DONE WHEN` shape as the build plan. Tasks are dependency-ordered (see Section 6).

### B ⛔ — Prefix-stable system prompt (the foundation)

**Actions:**
1. In `bot-nemotron.py`, audit every byte that goes into the system prompt. Move `date.today()`, `DEMO_NOW`, caller context out of the prefix.
2. Author the prompt as `<role> + <tool schemas> + <behavior rules>` — all static. Sort tool order deterministically.
3. Put dynamic context (caller info, current time) into a tool the LLM calls once at session start (`get_session_context()`), so it lives in the conversation history (cacheable per-call but not blocking prefix reuse across calls).
4. Add the `<conf>` instruction at the end of the static block (not in dynamic content) — it's the same every turn.

**Watch:** vLLM's prefix cache keys on the byte-identical prefix. ONE changed character invalidates the cache. Lint with `assert prompt == EXPECTED_PROMPT` in a unit test. This is the precondition that lets A and H inject per-turn signals without paying the cache miss.
**DONE WHEN:** system prompt is byte-identical across 5 consecutive calls; TTFT on turn 2 is measurably lower than turn 1.

### C ⛔ — Pre-cached TTS for fixed phrases

**Actions:**
1. Identify the always-spoken phrases: greeting (`"This is the on-call triage assistant, what's the symptom?"`), 5–6 tool fillers (`"pulling logs"`, `"checking deploys"`, `"looking at who's on shift"`), closing (`"Got it. RCA sent to your phone — go back to sleep."`).
2. At worker startup, call Gradium once per phrase, cache the raw audio bytes (PCM for WebRTC, μ-law for Twilio).
3. Replace the first TTS call on connect with a direct `OutputAudioRawFrame` push from cache. Hook into `FunctionCallInProgressFrame` to play tool-specific fillers.

**Watch:** different sample rates for WebRTC (24kHz) vs Twilio (8kHz μ-law). Cache both per phrase keyed on transport. Don't cache anything dynamic — keep the cache strictly for invariant strings.
**DONE WHEN:** first-audio latency on connect drops below 200ms (from ~1.2s baseline); tool-call dead air is filled within 200ms of the tool call firing.

### G ⛔ — Confidence + termination controller

**Actions:**
1. Add to the static system prompt: *"Before any tool call or final answer, emit exactly one line of the form `<conf>0.0</conf>` reflecting your certainty in the current root-cause hypothesis. Do not speak this aloud."*
2. In the LLM output processor, regex-extract `<conf>([\d.]+)</conf>` from the streaming response. Strip the tag from the content stream before it reaches TTS.
3. Write parsed value to `state.confidence`. Increment `state.turn_count` per LLM call. Track which tool categories have been called → `state.evidence_gathered`.
4. After each turn, run `state.should_terminate()` and `state.missing_evidence()`. Inject a system-role message:
   - terminate → `"Confidence is high. State the root cause concisely and propose remediation, then end."`
   - dig → `"Confidence still low after {turn_count} turns. You haven't checked: {missing}. Investigate at least one before concluding."`

**Watch:** the model will *sometimes* forget the `<conf>` tag — handle missing tag as "no update, last known value persists." Don't crash. Also: the model can game its own confidence — pair with `evidence_gathered` (low evidence + high conf = override to low).
**DONE WHEN:** `<conf>` values appear in 90%+ of turns; `state.confidence` visible in trace; E1 terminates ≥1 turn earlier; E2 digs deeper before concluding.

### A ⛔ — Per-turn thinking gate (the headline)

**Actions:**
1. Subclass `VLLMOpenAILLMService` → `AdaptiveThinkingLLMService`. Hold a reference to `state: InvestigationState`.
2. Override the per-request settings construction: before each call, read `state.needs_thinking()` and set `extra_body.chat_template_kwargs.enable_thinking` accordingly. Also force ON when the next tool call is `find_on_call_engineer` or when the agent is at the routing decision (track via a `state.about_to_route` flag set by the routing prompt branch).
3. Default OFF for conversational turns. Default ON for the *final* synthesis turn regardless of confidence (correctness > latency at the conclusion).
4. Log every decision: `[turn N | conf=0.X | thinking=ON|OFF | reason=...]` — this is the trace data you'll show on the slide.

**Watch:** if the hosted vLLM lacks `--reasoning-parser nemotron_v3`, thinking-on leaks chain-of-thought into `content` and gets spoken. Confirm with NVIDIA on-site BEFORE turning on; if the parser isn't there, use a fallback — emit thinking via a *separate prompted scratchpad* (`<scratch>...</scratch>` tag, filtered from TTS, no vLLM-side change). Less clean but works.
**DONE WHEN:** E2 and E4 (discrimination scenarios) PASS T1 with adaptive thinking ON for high-stakes turns; average thinking-token-spend per call is <40% of all-on baseline.

### H — Semantic incident match

**Actions:**
1. Pre-embed 4 incident symptom paraphrases × 3 each (12 vectors). Use `sentence-transformers/all-MiniLM-L6-v2` or any small embed model — pure local, no API call, ~80MB.
2. On each user turn, embed the utterance. Cosine-sim against the 12 vectors. If max > 0.85, set `state.incident_hypothesis = incident_id`.
3. Inject the hypothesis as a **synthetic `ToolResultFrame`** (`{"semantic_match": "incident_1", "confidence": 0.92, "note": "Symptoms resemble payments-db pool exhaustion. Verify before concluding."}`). It enters the conversation history, NOT the system prompt prefix.
4. The agent's prompt should be neutral about this hint — never instruct it to trust the hypothesis blindly. The hint biases prefetch (via E), not the conclusion.

**Watch:** wrong-match risk is real, especially on E2 (DB-pool symptom looks like #1 but is #2). The 0.85 threshold + "verify before concluding" framing is what keeps E2 from being trapped. Test E2 explicitly with H enabled — it must STILL pass, not regress.
**DONE WHEN:** E5 (vague opener) turns-to-resolution drops; E2 still passes T1 (hypothesis didn't mislead the agent).

### E — Speculative tool prefetch

**Actions:**
1. Wrap mock tool functions in a cache-aware executor that consults `state.prefetch_cache` before executing.
2. Add hooks on tool completion: when `get_alerts()` returns, kick off `asyncio.create_task(get_deploy_history())` and `asyncio.create_task(get_logs(alerted_service))` in the background, store results in `state.prefetch_cache`.
3. When `state.incident_hypothesis` is set by H, prefetch that incident's "typical evidence path" (e.g. incident #1 → also prefetch metrics for payments-db pool).
4. Prefetch cache keyed on `(tool_name, frozenset(args.items()))`. TTL = the entire call.

**Watch:** fixtures are local so wasted prefetch costs ~0ms — be aggressive. The only cost is code complexity. Don't prefetch on the routing tool (`find_on_call_engineer`) — its inputs depend on the LLM's classification, not pre-knowable.
**DONE WHEN:** average investigation turn count on E1 drops by ≥1 turn; investigation wall-time on E1 drops 2–4s.

---

## 5. The one LLM-side change that unifies G/A/H

You need confidence on every turn. **Do not add a tool for it** — that's a round-trip. Add this single line to the static system prompt (covered in G/B above, restated here because it's the keystone):

> *Before any tool call or final answer, emit exactly one line of the form `<conf>0.0</conf>` reflecting your certainty (0–1) in the current root-cause hypothesis. Do not speak this aloud — it is metadata.*

Then:
- The output parser writes `state.confidence` from this tag.
- G uses it for terminate/dig hints.
- A uses it for the thinking decision.
- H validates against it (if H matched a hypothesis but conf stays low after evidence, H's match was wrong — log this for the eval).

One prompt line, three optimizations consuming the signal. That's the leverage.

---

## 6. Build order (dependency-correct)

**Prerequisite (non-negotiable):** Section 0 gates met — stock agent works end-to-end, deployed, baseline eval captured. Only then start the sequence below.

You can't shuffle these without backtracking. The right sequence:

```
B (30 min) ──┐  B is the precondition: makes prefix stable
             │  so later per-turn injections from A/G/H don't
             │  blow up the prefix cache.
             ▼
C (20 min) ──┐  Independent. Build alongside B if pair-programming.
             ▼
G (45 min) ──┐  Confidence tracking is the smallest dynamic-state
             │  piece. Once it works, A has a signal to read.
             ▼
A (60 min) ──┐  Thinking gate consumes confidence from G.
             │  Verify the vLLM reasoning-parser story BEFORE this.
             ▼
H (60 min) ──┐  Embeddings + cosine + hypothesis injection.
             │  Easier once InvestigationState exists.
             ▼
E (90 min) ──┘  Speculative prefetch is last. Depends on H's
                hypothesis to bias prefetch and on tool executor
                reading from prefetch_cache.
```

**Total: ~5 hours.** Fits the Phase 1 + early Phase 2 window of the build plan (10:00 AM – 1:00 PM with buffer).

**Solo / time-boxed at 3 hours:** ship **B + C + G + A** and skip H + E. Still a coherent system (static prep + adaptive thinking based on confidence), still covers the headline accuracy + latency story. H + E are amplifiers.

---

## 7. Integration with the existing build plan

This loop is **post-MVP** (see Section 0). It does not splice into Phase 1 — it sits between C4 baseline and the freeze, or in Phase 4 Lane B. Each piece edits a file you already wrote during the core build:

| Optimization | File / build-plan task already in place | What you edit when applying |
|---|---|---|
| B | system prompt authored in I3 | rewrite for byte-stability; lint with equality assertion |
| C | TTS wired in I4 | add pre-cache step to worker startup; cache tool-filler clips |
| G | system prompt (I3) + LLM processor | append `<conf>` instruction; add output parser |
| A | LLM wiring (I2/I3) | subclass `VLLMOpenAILLMService` in `nemotron_llm.py`, gate on state |
| H | incident fixtures (I1) | extend fixtures with paraphrases; add embedding step + hint injector |
| E | mock tools (I2) | wrap tool functions in cache-aware executor |

**Strict sequencing (the safe path):**
1. Finish Phase 0 → 1 → 2 → 3 on the **stock agent**. Do not start this loop yet.
2. Run C4 baseline (Nemotron + GPT A/B). Capture numbers — that's the "before" column forever.
3. Apply this loop in dependency order (Section 6): B → C → G → A → H → E, each behind its own env toggle.
4. After each piece lands, re-run the affected eval scenarios as a sanity check (B → spot-check TTFT; C → spot-check first-audio; A → re-run E2/E4; H → re-run E5; E → re-run E1).
5. Once all six are live and toggle-tested, run the full Cekura suite again — that's the "after" column.
6. Hand to the Cekura self-improving loop (A2) so it iterates on the *optimized* floor, not the stock one. Higher floor → more visible curve.

**Anti-pattern to avoid:** applying optimizations in parallel with Phase 1 development. You'll spend the afternoon debugging "is this a triage bug or an optimization bug?" instead of measuring.

---

## 8. Headline measurement (the slide)

Run the **same Cekura suite** (E1–E9) against two configurations: (1) stock starter, (2) concentrated loop. Same model, same TTS, same eval scenarios. Two columns:

| Metric | Stock | Concentrated | Driver |
|---|---|---|---|
| First-audio latency (call open) | ~1200ms | ~150ms | C |
| Turn-2+ TTFT (cache hit) | ~600ms | ~250ms | B |
| Average tool round-trips per investigation | 5 | 3 | E + H |
| E1 investigation time (start → conclusion) | ~14s | ~8s | E + G |
| E2 T1 root-cause accuracy (discrimination trap) | FAIL | PASS | A (thinking on when conf low) |
| E4 T1 root-cause accuracy | FAIL | PASS | A |
| E5 turns-to-resolution (vague opener) | ~7 | ~4 | H (hypothesis from fuzzy match) |
| Average thinking-token-spend per call | all-on (slow) OR all-off (wrong) | ~30% of turns | A |
| Average call wall-time (E1+E3+E5) | baseline | -40% | composition |

Every row maps to a Cekura metric you already authored. The eval suite **backs the latency story** — they're not two workstreams.

---

## 9. Cut list (under time pressure)

In priority order, what to drop if the day compresses:

1. **Drop E first.** Speculative prefetch is the highest-effort, lowest-clarity-of-demo piece. Cutting it loses 2 round-trips on E1 but preserves the headline accuracy + latency story.
2. **Drop H second.** Without it, E5 still works (just slower). A and G still function — they just lose one signal.
3. **Never drop B.** It's the foundation. Without B, A and H actively *cost* you latency (prefix cache miss on every turn).
4. **Never drop A.** It's the headline. Without A, you have no answer to E2/E4 discrimination failures.
5. **Never drop C.** 20 minutes for the most visible latency win in the demo.

So the irreducible minimum is **B + C + G + A** (~2h45m). G can be a half-implementation (just track turns and evidence, skip the `<conf>` tag) if needed — A will still work with confidence stubbed to 0.5 default.

---

## 10. The 90-second pitch (for the judges' slide)

> We composed six optimizations into one loop. Static prep — pre-rendered TTS, frozen prompt prefix, pre-embedded incidents — runs at deploy time so the call starts hot. Per turn, we track confidence and evidence in shared state. A semantic match against past incidents seeds a hypothesis we can confirm or reject; that hypothesis biases what we prefetch in the background, so by the time the LLM asks for the deploy log it's already cached. The model's own confidence drives a thinking gate — we burn reasoning compute only when it matters: on turns four-plus when we're still uncertain, or right before a routing decision. We terminate when confidence converges and dig deeper when it doesn't. Same Nemotron, same Gradium, same Pipecat — four-times faster on first-audio, two-times faster on TTFT, and we pass the discrimination scenarios the stock starter fails.

---

## 11. What this does NOT claim

To stay honest in front of judges who will press on details:

- **Not a serving optimization.** We don't touch vLLM, the model, or the GPUs. We're a smarter client.
- **Not a guarantee.** A wrong H-match can mislead the agent; we mitigate with the 0.85 threshold and "verify before concluding" framing, but it's a tradeoff.
- **Not free of failure modes.** Per-turn thinking-toggle assumes the vLLM has a reasoning parser to keep CoT out of `content`. If not, we fall back to scratchpad-tag filtering (less clean).
- **Not novel ML.** Every piece is an existing technique (prefix caching, speculative execution, semantic retrieval, adaptive compute). The novelty is the *composition* and the *measurement against an adversarial eval*.

The system claim is composition + measurement. That's the defensible thing.
