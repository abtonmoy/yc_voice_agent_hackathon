'use strict';
const assert = require('assert');
const { initialState, reduce } = require('./app.js');

let s = initialState();
[1890, 3880, 2950, 1126].forEach(ms => { s = reduce(s, { id: 'l' + ms, type: 'latency', payload: { metric: 'v2v', ms } }); });
s = reduce(s, { id: 'llm', type: 'latency', payload: { metric: 'llm_ttfb', ms: 632 } });
s = reduce(s, { id: 'tts', type: 'latency', payload: { metric: 'tts_ttfb', ms: 357 } });
s = reduce(s, { id: 'ck', type: 'cekura', payload: { pass: 8, total: 8, runs: [{ name: 'approve', passed: true }, { name: 'decline', passed: true }] } });

assert.strictEqual(s.metrics.v2v.length, 4, 'v2v curve should have 4 turns');
assert.strictEqual(s.metrics.llm[0], 632, 'llm waterfall');
assert.strictEqual(s.metrics.tts[0], 357, 'tts waterfall');
assert(s.metrics.cekura && s.metrics.cekura.pass === 8 && s.metrics.cekura.total === 8, 'cekura pass/total');
assert.strictEqual(s.header.v2v, '1.13', 'header v2v = latest (1126ms)');
assert.strictEqual(s.status.cekura, '8/8', 'status cekura');
assert.strictEqual(s.trace.length, 0, 'latency/cekura must NOT add trace lines');

console.log('METRICS TEST PASS — v2v', s.metrics.v2v.length, 'turns | llm', s.metrics.llm[0], 'ms | tts', s.metrics.tts[0], 'ms | cekura', s.status.cekura);
