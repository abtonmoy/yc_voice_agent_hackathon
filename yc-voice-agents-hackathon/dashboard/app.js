'use strict';

/* ════════════════════════════════════════════════════════════════════
   PURE CORE  — node-testable, NO DOM, NO browser globals.
   Folds event envelopes into state. Every event type in the fixture is
   handled. Functions never mutate their input state (return new state).
   ════════════════════════════════════════════════════════════════════ */

function initialState() {
  return {
    header:    { incident: null, service: null, session: null, v2v: null },
    trace:     [],
    code:      { file: null, status: 'idle', before: '', after: '', diff: '' },
    status:    { paged: null, v2v: null, cekura: null },
    metrics:   { v2v: [], llm: [], tts: [], cekura: null },
    sessionId: null,
  };
}

/* §5.2 glyph map + line classes — pure formatter. */
function traceLine(evt) {
  const type = evt.type;
  const p = evt.payload || {};

  switch (type) {
    case 'transcript': {
      if (p.role === 'user')
        return { glyph: 'you ›', cls: 't-you', text: p.text || '' };
      return { glyph: 'agent ‹', cls: 't-agent', text: p.text || '' };
    }
    case 'tool_call': {
      const args = formatArgs(p.args);
      return { glyph: '→', cls: 't-tool', text: `${p.tool}(${args})` };
    }
    case 'tool_result':
      return { glyph: '↳', cls: 't-result', text: p.summary || '' };
    case 'alert':
      return {
        glyph: '⚠ ALERT',
        cls: 't-alert',
        text: `${p.title || ''}${p.service ? '  ' + p.service : ''}${p.severity ? ' · ' + p.severity : ''}${p.started_at ? ' @' + p.started_at : ''}`,
      };
    case 'hypothesis': {
      const conf = (p.confidence !== undefined && p.confidence !== null) ? p.confidence + ' · ' : '';
      return { glyph: '~', cls: 't-hyp', text: `hypothesis ${conf}${p.text || p.hypothesis || ''}`.trim() };
    }
    case 'rca':
      return { glyph: '■ ROOT CAUSE', cls: 't-rca', text: p.root_cause || '' };
    case 'routing_decision': {
      const c = p.chosen || {};
      const loc = c.location ? c.location : '';
      const lt = c.local_time ? ' ' + c.local_time : '';
      const why = p.reason ? ' · ' + p.reason : '';
      return { glyph: '☎ PAGE', cls: 't-page', text: `→ ${c.name || ''} · ${loc}${lt}${why}` };
    }
    case 'outbound_call':
      return { glyph: '☎', cls: 't-tool', text: `${p.engineer || p.engineer_id || ''} · ${p.status || ''}` };
    case 'fix_proposed':
      return { glyph: '± propose', cls: 't-propose', text: `propose fix · ${p.file || ''}` };
    case 'fix_decision':
      return p.approved
        ? { glyph: '✓ approved', cls: 't-approved', text: 'approved' }
        : { glyph: '✗ declined', cls: 't-declined', text: 'declined, no change' };
    case 'code_fix':
      return { glyph: '✎ applied', cls: 't-applied', text: `applied · ${p.file || ''}` };
    case 'session_start':
      return { glyph: '·', cls: 't-end', text: `session ${p.session_id || ''} (${p.backend || ''})` };
    case 'session_end':
      return { glyph: '·', cls: 't-end', text: `session ended${p.reason ? ' · ' + p.reason : ''}` };
    default:
      return { glyph: '·', cls: 't-result', text: p.summary || p.text || type };
  }
}

function formatArgs(args) {
  if (!args || typeof args !== 'object') return '';
  const keys = Object.keys(args);
  if (keys.length === 0) return '';
  return keys.map(k => String(args[k])).join(', ');
}

/* Parse a unified diff into {before, after}. Handles the fixture quirk
   where the removed (`-`) and added (`+`) body lines are concatenated
   on a single physical line: "-OLD+NEW". */
function parseDiff(diff) {
  let before = '', after = '';
  if (!diff) return { before, after };
  const lines = diff.split('\n');
  const adds = [], dels = [];
  for (const raw of lines) {
    if (raw.startsWith('---') || raw.startsWith('+++') || raw.startsWith('@@')) continue;
    if (raw.startsWith('-')) {
      const plus = raw.indexOf('+');           // joined "-OLD+NEW" case
      if (plus > 0) {
        dels.push(raw.slice(1, plus));
        adds.push(raw.slice(plus + 1));
      } else {
        dels.push(raw.slice(1));
      }
    } else if (raw.startsWith('+')) {
      adds.push(raw.slice(1));
    }
  }
  before = dels.join('\n');
  after = adds.join('\n');
  return { before, after };
}

/* reduce(state, evt) → next state for ONE event envelope. */
function reduce(state, evt) {
  if (!evt || !evt.type) return state;
  const p = evt.payload || {};
  const next = {
    header:    { ...state.header },
    trace:     state.trace.slice(),
    code:      { ...state.code },
    status:    { ...state.status },
    metrics:   { v2v: state.metrics.v2v.slice(), llm: state.metrics.llm.slice(), tts: state.metrics.tts.slice(), cekura: state.metrics.cekura },
    sessionId: state.sessionId,
  };

  // Most events append a trace line; the few that don't are excluded below.
  const NO_TRACE = new Set(['latency', 'cekura']);
  if (!NO_TRACE.has(evt.type)) {
    next.trace.push({ ...traceLine(evt), id: evt.id, seq: evt.seq });
  }

  switch (evt.type) {
    case 'session_start':
      next.sessionId = p.session_id || null;
      next.header.session = p.session_id || null;
      break;

    case 'alert':
      next.header.incident = p.title || next.header.incident;
      next.header.service = p.service || next.header.service;
      break;

    case 'routing_decision': {
      const c = p.chosen || {};
      next.status.paged = { id: c.id || null, name: c.name || null, location: c.location || null, local_time: c.local_time || null, status: null };
      break;
    }

    case 'outbound_call':
      if (next.status.paged) {
        next.status.paged = { ...next.status.paged, status: p.status || null };
        if (p.engineer && !next.status.paged.name) next.status.paged.name = p.engineer;
      } else {
        next.status.paged = { id: p.engineer_id || null, name: p.engineer || null, location: null, local_time: null, status: p.status || null };
      }
      break;

    case 'fix_proposed': {
      next.code.file = p.file || next.code.file;
      next.code.status = 'proposed';
      next.code.diff = p.diff || '';
      if (p.before !== undefined || p.after !== undefined) {
        next.code.before = p.before || '';
        next.code.after = p.after || '';
      } else {
        const parsed = parseDiff(p.diff);
        next.code.before = parsed.before;
        next.code.after = parsed.after;
      }
      break;
    }

    case 'fix_decision':
      if (p.approved === false) next.code.status = 'declined';
      break;

    case 'code_fix': {
      next.code.file = p.file || next.code.file;
      next.code.status = p.applied ? 'applied' : next.code.status;
      next.code.diff = p.diff || next.code.diff;
      if (p.before !== undefined) next.code.before = p.before;
      if (p.after !== undefined) next.code.after = p.after;
      if (p.before === undefined && p.after === undefined && p.diff) {
        const parsed = parseDiff(p.diff);
        next.code.before = parsed.before;
        next.code.after = parsed.after;
      }
      break;
    }

    case 'latency': {
      // accept ms (the tap) or value/seconds (legacy); accumulate by metric.
      const ms = (p.ms != null) ? p.ms
               : (p.value != null) ? p.value * 1000
               : (p.seconds != null) ? p.seconds * 1000 : null;
      const metric = p.metric || 'v2v';
      if (ms != null) {
        if (metric === 'v2v') {
          next.metrics.v2v.push(ms);
          next.header.v2v = (ms / 1000).toFixed(2);
          next.status.v2v = next.header.v2v;
        } else if (metric === 'llm_ttfb' || metric === 'llm') {
          next.metrics.llm.push(ms);
        } else if (metric === 'tts_ttfb' || metric === 'tts') {
          next.metrics.tts.push(ms);
        }
      }
      break;
    }

    case 'cekura':
      if (p.pass != null && p.total != null) {
        next.metrics.cekura = { pass: p.pass, total: p.total, runs: p.runs || [] };
        next.status.cekura = `${p.pass}/${p.total}`;
      } else {
        next.status.cekura = p.pass_rate || p.summary || next.status.cekura;
      }
      break;

    case 'session_end':
      next.header.session = next.header.session;
      break;
  }

  return next;
}

/* Node export — browsers ignore this. */
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { initialState, reduce, traceLine, parseDiff };
}

/* ════════════════════════════════════════════════════════════════════
   DOM / BROWSER LAYER  — guarded so requiring app.js in node is inert.
   ════════════════════════════════════════════════════════════════════ */
if (typeof document !== 'undefined') {
  (function () {
    let state = initialState();
    let prevCodeStatus = 'idle';
    const seen = new Set();

    const $ = (id) => document.getElementById(id);

    function render(s) {
      // ── HEADER ──
      const inc = s.header.incident
        ? (s.header.service ? `${s.header.incident} · ${s.header.service}` : s.header.incident)
        : '—';
      $('hdr-incident').textContent = inc;
      $('hdr-session').textContent = s.header.session || '—';
      $('hdr-v2v').textContent = s.header.v2v != null ? `V2V ${s.header.v2v}s` : 'V2V —';

      // ── TRACE ──
      renderTrace(s.trace);

      // ── CODE HERO ──
      renderCode(s.code);

      // ── STATUS STRIP ──
      renderStatus(s);

      // ── METRICS (curves) ──
      renderMetrics(s.metrics);
    }

    function avg(xs) { return xs.length ? Math.round(xs.reduce((a, b) => a + b, 0) / xs.length) : 0; }

    function renderMetrics(m) {
      // V2V latency curve — one bar per agent turn
      const cur = $('lat-curve');
      if (m.v2v.length) {
        const max = Math.max(...m.v2v, 1);
        cur.innerHTML = '';
        m.v2v.forEach(ms => {
          const b = document.createElement('div');
          b.className = 'bar';
          b.style.height = Math.max(2, Math.round(ms / max * 50)) + 'px';
          b.title = ms + ' ms';
          cur.appendChild(b);
        });
        $('lat-stat').textContent = `avg ${avg(m.v2v)} ms · ${m.v2v.length} turns`;
      }
      // component waterfall
      const wf = $('waterfall');
      const comps = [['LLM TTFB', m.llm], ['TTS TTFB', m.tts]];
      const cmax = Math.max(...m.llm, ...m.tts, 1);
      const rows = comps.filter(([, arr]) => arr.length);
      if (rows.length) {
        wf.innerHTML = '';
        rows.forEach(([label, arr]) => {
          const a = avg(arr);
          const row = document.createElement('div'); row.className = 'wf-row';
          const l = document.createElement('span'); l.className = 'wf-label'; l.textContent = label;
          const bar = document.createElement('span'); bar.className = 'wf-bar';
          bar.style.width = Math.max(6, Math.round(a / cmax * 120)) + 'px';
          const v = document.createElement('span'); v.className = 'wf-val'; v.textContent = a + ' ms';
          row.append(l, bar, v); wf.appendChild(row);
        });
      }
      // Cekura pass-dots
      const cd = $('cekura-dots');
      if (m.cekura) {
        const runs = (m.cekura.runs && m.cekura.runs.length)
          ? m.cekura.runs
          : Array.from({ length: m.cekura.total }, (_, i) => ({ passed: i < m.cekura.pass }));
        cd.innerHTML = '';
        runs.forEach(r => {
          const d = document.createElement('div');
          d.className = 'dot ' + (r.passed ? 'pass' : 'fail');
          d.title = r.name || '';
          cd.appendChild(d);
        });
        $('cekura-rate').textContent = `${m.cekura.pass}/${m.cekura.total}`;
      }
    }

    function renderTrace(trace) {
      const feed = $('trace-feed');
      // append only new lines (keyed by id) to preserve scroll + animations
      const existing = feed.childElementCount;
      for (let i = existing; i < trace.length; i++) {
        const ln = trace[i];
        const row = document.createElement('div');
        row.className = 'tline ' + ln.cls;
        const g = document.createElement('span');
        g.className = 'glyph';
        g.textContent = ln.glyph;
        const t = document.createElement('span');
        t.className = 'text';
        t.textContent = ln.text;
        row.appendChild(g);
        row.appendChild(t);
        feed.appendChild(row);
      }
      // autoscroll if user is near the bottom
      const tr = $('trace');
      const nearBottom = tr.scrollHeight - tr.scrollTop - tr.clientHeight < 80;
      if (nearBottom) tr.scrollTop = tr.scrollHeight;
    }

    function renderCode(code) {
      $('code-file').textContent = code.file || '—';
      const tag = $('code-tag');
      if (code.status === 'applied') { tag.textContent = '✎ applied'; tag.className = 'code-tag'; }
      else if (code.status === 'declined') { tag.textContent = '— declined, no change'; tag.className = 'code-tag declined'; }
      else if (code.status === 'proposed') { tag.textContent = 'proposed'; tag.className = 'code-tag declined'; }
      else { tag.textContent = ''; tag.className = 'code-tag'; }

      const pre = $('code-diff');

      if (code.status === 'idle' || (!code.before && !code.after)) {
        if (pre.dataset.rendered !== 'idle') { pre.innerHTML = ''; pre.dataset.rendered = 'idle'; }
        return;
      }

      // (Re)build diff rows when status changes.
      const key = code.status + '|' + code.before + '|' + code.after;
      if (pre.dataset.key === key) return;
      pre.dataset.key = key;

      const pending = (code.status === 'proposed' || code.status === 'declined');
      pre.innerHTML = '';
      splitLines(code.before).forEach(line => {
        const el = makeLine('− ' + line, 'diff-del');
        if (pending) el.classList.add('pending');
        if (code.status === 'applied') el.classList.add('collapsing');
        pre.appendChild(el);
      });
      splitLines(code.after).forEach(line => {
        const el = makeLine('+ ' + line, 'diff-add');
        if (pending) el.classList.add('pending');
        if (code.status === 'applied') el.classList.add('applied');   // §6 applyFlash
        pre.appendChild(el);
      });

      // when applied, remove collapsed del rows after the animation
      if (code.status === 'applied') {
        setTimeout(() => {
          pre.querySelectorAll('.diff-del.collapsing').forEach(n => n.remove());
        }, 600);
      }
    }

    function makeLine(text, cls) {
      const el = document.createElement('span');
      el.className = 'dl ' + cls;
      el.textContent = text;
      return el;
    }
    function splitLines(s) {
      if (!s) return [];
      return s.split('\n');
    }

    function renderStatus(s) {
      // logs tail = last tool_result text
      let lastResult = '';
      for (let i = s.trace.length - 1; i >= 0; i--) {
        if (s.trace[i].cls === 't-result') { lastResult = s.trace[i].text; break; }
      }
      if (lastResult) $('logs-tail').textContent = lastResult;

      const dot = $('paged-dot');
      const txt = $('paged-text');
      if (s.status.paged) {
        const pg = s.status.paged;
        const loc = pg.location ? ` · ${pg.location}${pg.local_time ? ' ' + pg.local_time : ''}` : '';
        txt.textContent = `☎ ${pg.name || ''}${loc}`;
        dot.className = 'paged-dot';
        if (pg.status === 'ringing') dot.classList.add('ringing');
        else if (pg.status === 'answered') dot.classList.add('answered');
        else dot.classList.add('answered');
      } else {
        txt.textContent = '—';
        dot.className = 'paged-dot';
      }

      $('cekura-text').textContent = s.status.cekura != null ? s.status.cekura : '—';
    }

    function connect() {
      const dot = $('conn-dot');
      const es = new EventSource(RELAY_URL + '/stream');
      es.onopen = () => { dot.className = 'conn-dot live'; };
      es.onmessage = (m) => {
        let evt;
        try { evt = JSON.parse(m.data); } catch (e) { return; }
        if (evt.id && seen.has(evt.id)) return;     // dedupe replayed buffer
        if (evt.id) seen.add(evt.id);
        state = reduce(state, evt);
        render(state);
      };
      es.onerror = () => { dot.className = 'conn-dot down'; };  // EventSource auto-reconnects
    }

    render(state);
    connect();
  })();
}
