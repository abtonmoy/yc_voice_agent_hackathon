# LLD — Frontend (Mission-Control Dashboard)

Minimalist black-and-white dashboard that makes the agent's autonomous loop **visible** while it's **audible**. Consumes the event stream from the relay (`lld-backend.md` §3, §9). No build step, no framework.

**Design law:** *legible-slightly-behind beats pretty-janky.* One thing hero at a time. The only "motion" is the code-fix highlight.

---

## 1. Design language — light monochrome + one accent

Light/paper base, near-black ink, and **a single accent** reserved for *live + resolution* states. Everything else is grayscale.

| token | value | use |
|---|---|---|
| `--paper` | `#fcfcfc` | background (warm near-white) |
| `--ink` | `#141414` | primary text |
| `--ink-dim` | `#6b6b6b` | secondary / older lines |
| `--ink-faint` | `#cfcfcf` | borders, rules, logs |
| `--accent` | `#1a7f37` | **the one accent** — swap this single token to recolor |
| `--accent-soft` | `rgba(26,127,55,.16)` | transient code-fix flash background |
| font | `ui-monospace, "JetBrains Mono", "SF Mono", monospace` | everything |
| base size | `18px` (projector-legible), code `16px` | |

**Accent is rationed to "live / resolved" only** — added/applied diff lines, the apply flash, the `approved` marker, and the live-call dot (ringing/answered). **Problem-side markers (ALERT, ROOT CAUSE) stay bold ink**, not accent — so the eye reads *black = the investigation, accent = the fix landing*. Diff semantics:
- added line → `+` prefix, **`--accent`, bold**, brief `--accent-soft` flash on apply.
- removed line → `−` prefix, `--ink-dim`, `line-through`, then collapses (stays grayscale — only the *fix* gets color).

---

## 2. Layout (CSS grid)

```
┌──────────────────────────────────────────────────────────────┐
│ HEADER  inc-1 · payments-api  •  sess_ab12  •  V2V 1.21s       │  56px
├───────────────────────────────┬──────────────────────────────┤
│ TRANSCRIPT / TRACE            │  CODE — app/db.py             │
│ (scrolling feed, autoscroll)  │  (hero: file → live diff)     │  1fr
│                               │                               │
├───────────────────────────────┴──────────────────────────────┤
│ LOGS ▏ …tailing…   │  PAGED ☎ Priya · London 11:00  │ CEKURA 7/9 │  64px
└──────────────────────────────────────────────────────────────┘
```
```css
body{display:grid;grid-template-rows:56px 1fr 64px;grid-template-columns:minmax(380px,1fr) 1.2fr;
     grid-template-areas:"header header" "trace code" "status status";
     background:var(--paper);color:var(--ink);height:100vh;margin:0}
```
On a narrow screen, stack `trace` over `code` (media query) — but the demo target is a wide projector.

---

## 3. Tech & files

```
dashboard/
  index.html     # the 4 regions
  style.css      # tokens + grid + diff animation
  app.js         # EventSource client, state, renderers
```
- Serve with `python -m http.server` or let the relay serve it statically. Open in a full-screen browser tab.
- **No React/bundler.** Vanilla DOM. ~300 lines of JS total.

---

## 4. Data flow

```js
const es = new EventSource(`${RELAY_URL}/stream`);      // backlog replayed on connect
const seen = new Set();
es.onmessage = (m) => {
  const evt = JSON.parse(m.data);
  if (seen.has(evt.id)) return;            // dedupe (relay replays buffer on reconnect)
  seen.add(evt.id);
  (RENDERERS[evt.type] || noop)(evt.payload);
};
es.onerror = () => {/* EventSource auto-reconnects; show a dim "reconnecting" dot */};
```
- `EventSource` auto-reconnects and the relay replays its ring buffer, so a dropped connection self-heals. `seq`/`id` dedupe prevents double-render.
- **Replay/backup mode (F2):** point `RELAY_URL` at the relay's `/replay` (streams `events.ndjson` with original timing) — the dashboard can re-run a recorded session identically for the safety video.

---

## 5. Components

### 5.1 Header
`inc-1 · payments-api  •  sess_ab12  •  V2V 1.21s`
- Updates `incident`/`session` on `session_start`; `V2V` from the latest `latency{metric:"v2v"}` (number only — the full waterfall stays a static slide).

### 5.2 Transcript / Trace (left)
An ordered, autoscrolling feed. Each event → one line with a **monochrome glyph** and weight; older lines fade to `--ink-dim`.

| event | rendered line |
|---|---|
| `transcript` user | `you ›  payments api is throwing 500s` |
| `transcript` agent | `agent ‹ checking recent deploys…` |
| `tool_call` | `  →  get_logs(payments-api)` (dim) |
| `tool_result` | `  ↳  200× connection-refused @14:32` (dim) |
| `hypothesis` | `  ~  hypothesis 0.8 · pool exhausted` |
| `rca` | `  ■  ROOT CAUSE  deploy abc123 → db pool` (bold) |
| `routing_decision` | `  ☎  PAGE → Priya · London 11:00 · owns db` (bold) |
| `fix_proposed` | `  ±  propose fix · app/db.py` |
| `fix_decision` | `  ✓  approved` / `  ✗  declined` |
| `code_fix` | `  ✎  applied · app/db.py` |

- Autoscroll to bottom unless the user has scrolled up (then show a "● new" affordance).
- `routing_decision` may expand on hover to list candidates with `excluded_reason` (asleep / wrong team) — optional.

### 5.3 Code panel (right, HERO) — the live diff
States, driven by events:

1. **Idle** — empty or shows the file once `read_repo_file`/`fix_proposed` names it: header `CODE — app/db.py`.
2. **Proposed** (`fix_proposed`) — render the diff, but **pending**: removed lines dim + `line-through`, added lines shown at `--ink-dim` with a dashed left-border (not yet committed). Conveys "waiting for the human."
3. **Applied** (`code_fix`, `applied:true`) — play the **apply animation** (§6): removed lines collapse out, added lines settle to `--accent` (bold) with the accent flash. Header tag flips to `CODE — app/db.py ✎ applied`.
4. **Declined** (`fix_decision`, `approved:false`) — revert pending styling, stamp `— declined, no change` in `--ink-dim`. **Demo the safety beat: nothing mutates.**

Render uses the `before`/`after`/`diff` in the event payload — **no filesystem access needed**.

### 5.4 Status strip (bottom)
- **LOGS** — a slow horizontal tail of recent `tool_result`/log lines, `--ink-faint`, non-interactive atmosphere.
- **PAGED** — chip from `routing_decision`/`outbound_call`: `☎ Priya · London 11:00` + an `--accent` status dot (ringing = pulsing accent, answered = solid accent).
- **CEKURA** — optional `pass_rate` (`7/9`) from a `cekura` event or a static value. *Primary Cekura curve is shown on its own dashboard tab — not rebuilt here.*

---

## 6. The code-fix highlight ("while it's happening")

The one intentional animation. Pure grayscale, ≤1.2s, never obscures text.

```css
.diff-add{color:var(--accent)}
@keyframes applyFlash{
  0%   {background:var(--accent-soft); box-shadow:inset 3px 0 0 var(--accent)}
  100% {background:transparent;        box-shadow:inset 3px 0 0 transparent}
}
.diff-add.applied{animation:applyFlash 1.2s ease-out forwards;font-weight:700}
.diff-del{color:var(--ink-dim);text-decoration:line-through}
.diff-del.collapsing{animation:collapse .5s ease-in forwards}     /* height→0, opacity→0 */
@keyframes collapse{to{height:0;opacity:0;margin:0;padding:0}}
.diff-add.pending,.diff-del.pending{opacity:.55}
.diff-add.pending{color:var(--ink-dim);border-left:2px dashed var(--ink-faint)}  /* no accent until applied */
```
> The accent only appears the instant the fix is **applied** — pending added lines are grayscale, so the color *is* the "it just happened" signal.
Sequence on `code_fix`:
1. removed lines → add `.collapsing` (slide/fade out).
2. added lines → swap `.pending`→`.applied` → `applyFlash` plays (background pulse + left border draws, then fades).
3. (optional) typewriter-reveal the added line text for ~400ms before the flash for extra "it's happening" feel — skip if it risks jank.

Result: the moment the engineer says "yes," the buggy line visibly **collapses out** and the fix **flashes in** — synced to the agent saying "done."

---

## 7. app.js skeleton

```js
const state = { file:null, diff:null };
const RENDERERS = {
  session_start: p => header.set(p),
  transcript:    p => trace.line(p.role, p.text),
  tool_call:     p => trace.tool(p.tool, p.args),
  tool_result:   p => { trace.result(p.summary); logs.push(p.summary); },
  alert:         p => trace.alert(p),
  hypothesis:    p => trace.hyp(p),
  rca:           p => trace.rca(p),
  routing_decision: p => { trace.page(p.chosen, p.reason); status.paged(p.chosen); },
  outbound_call: p => status.callStatus(p.status),
  fix_proposed:  p => code.propose(p.file, p.diff),     // -> pending state
  fix_decision:  p => p.approved ? null : code.decline(),
  code_fix:      p => code.apply(p),                    // -> applyFlash
  latency:       p => header.latency(p),
  session_end:   p => trace.end(p),
};
```
`code.propose/apply/decline` are the only stateful renderers; the rest append to the trace or update a chip.

---

## 8. Resilience & legibility checklist
- ☐ Auto-reconnect (EventSource) + buffer replay + id dedupe — survives a wifi blip mid-demo.
- ☐ Replay mode from `events.ndjson` for the backup recording.
- ☐ Font ≥16px, max contrast, generous line-height — readable from the back row.
- ☐ Animations ≤1.2s and only on the diff — everything else is instant.
- ☐ Don't narrate the dashboard live; let it corroborate the voice.

## 9. Build order (partial = still useful)
1. **Trace panel + EventSource** — ship this alone and the voice is already legible.
2. **Code panel: propose → apply highlight** — the hero.
3. **Status strip (paged chip + V2V) + logs tail** — cheap polish.
4. **Cekura** — open its own dashboard; optional one number here.
```
