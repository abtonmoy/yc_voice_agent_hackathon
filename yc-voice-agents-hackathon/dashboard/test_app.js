'use strict';

/* Node test — no deps. Folds the 31 fixture events through reduce() and
   asserts the FINAL state matches the contract. Run: node dashboard/test_app.js */

const fs = require('fs');
const path = require('path');
const { initialState, reduce, traceLine } = require('./app.js');

function assert(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg); process.exit(1); }
}

const ndjson = fs.readFileSync(path.join(__dirname, 'fixtures', 'events.ndjson'), 'utf8');
const events = ndjson.split('\n').map(l => l.trim()).filter(Boolean).map(l => JSON.parse(l));

assert(events.length === 31, `expected 31 events, got ${events.length}`);

// purity: reduce must not throw on ANY event type, and must not mutate input
let state = initialState();
const frozenStart = initialState();
for (const evt of events) {
  assert(typeof traceLine(evt) === 'object', `traceLine threw/empty for ${evt.type}`);
  state = reduce(state, evt);
}
// input-not-mutated check
assert(frozenStart.trace.length === 0, 'reduce mutated a prior state.trace');
assert(frozenStart.code.status === 'idle', 'reduce mutated a prior state.code');

// ── FINAL state assertions ──
assert(state.code.status === 'applied', `code.status should be applied, got ${state.code.status}`);
assert(state.code.file === 'app/db.py', `code.file should be app/db.py, got ${state.code.file}`);
assert(/pool_size=20/.test(state.code.after), `code.after should contain pool_size=20, got: ${state.code.after}`);
// rendered "after" must NOT still show the buggy before line
assert(!/no pool cap/.test(state.code.after), `code.after still shows buggy before line: ${state.code.after}`);
assert(/no pool cap/.test(state.code.before), `code.before should retain the buggy line`);

assert(state.status.paged && state.status.paged.name === 'Fardin', `status.paged.name should be Fardin, got ${state.status.paged && state.status.paged.name}`);
assert(state.status.paged.status === 'answered', `paged status should be answered, got ${state.status.paged.status}`);

// trace must contain ROOT CAUSE, PAGE, and applied lines
const hasRca = state.trace.some(l => l.cls === 't-rca' && /ROOT CAUSE/.test(l.glyph));
const hasPage = state.trace.some(l => l.cls === 't-page' && /PAGE/.test(l.glyph));
const hasApplied = state.trace.some(l => l.cls === 't-applied' && /applied/.test(l.text));
assert(hasRca, 'trace missing ROOT CAUSE line');
assert(hasPage, 'trace missing PAGE line');
assert(hasApplied, 'trace missing applied line');

assert(state.trace.length > 15, `trace should have > 15 entries, got ${state.trace.length}`);

assert(state.header.incident === 'payments-api 5xx >20%', `header.incident wrong: ${state.header.incident}`);
assert(state.sessionId === 'sess_e1_demo', `sessionId wrong: ${state.sessionId}`);

console.log('PASS — all assertions held');
console.log('  trace entries   :', state.trace.length);
console.log('  code.status     :', state.code.status);
console.log('  code.file       :', state.code.file);
console.log('  code.after      :', state.code.after);
console.log('  paged           :', state.status.paged.name, '·', state.status.paged.location, state.status.paged.local_time, '·', state.status.paged.status);
console.log('  header.incident :', state.header.incident, '·', state.header.service);
console.log('  sessionId       :', state.sessionId);
process.exit(0);
