import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import {buildRuntimeSource} from './transport_session.mjs';

const CORE = '/Applications/Fixture.app/Contents/Resources/wechat.dylib';
const RUNTIME = `
globalThis.initialized = (globalThis.initialized || 0) + 1;
if (globalThis.badUuid) throw Error('Loaded image UUID does not match adapter; restart/version selection required');
rpc.exports = {status(){},probe(){},sendText(){},sendImage(){},sendVoice(){}};
`;

function harness(modules) {
  const state = {modules, time: 0, pending: [], events: []};
  const context = {Process: {enumerateModules: () => state.modules}, rpc: {},
    Date: {now: () => state.time},
    setTimeout: (callback, delay) => state.pending.push({callback, at: state.time + delay}),
    send: value => state.events.push(value)};
  function load() {vm.runInNewContext(buildRuntimeSource({core_path: CORE}, RUNTIME), context);}
  function advance(until) {
    while (state.pending.length && state.pending[0].at <= until) {
      const next = state.pending.shift(); state.time = next.at; next.callback();
    }
    state.time = until;
  }
  return {state, context, load, advance};
}

test('mapped core initializes synchronously with no readiness timers', () => {
  const h = harness([{path: CORE}]); h.load();
  assert.equal(h.context.initialized, 1);
  assert.equal(h.state.pending.length, 0);
  assert.equal(h.state.events.length, 1);
  assert.equal(h.state.events[0].kind, 'rea_runtime_ready');
  assert.equal(h.state.events[0].wait_ms, 0);
});

test('a not-yet-mapped core waits then validates once before exports are ready', () => {
  const h = harness([]); h.load(); h.advance(200);
  assert.equal(h.context.initialized, undefined);
  assert.equal(h.state.events.length, 1);
  assert.equal(h.state.events[0].kind, 'rea_runtime_waiting');
  h.state.modules = [{path: CORE}]; h.advance(300);
  assert.equal(h.context.initialized, 1);
  assert.equal(h.state.events[1].kind, 'rea_runtime_ready');
  assert.equal(h.state.events[1].wait_ms, 300);
  assert.equal(h.state.pending.length, 0);
});

test('missing core times out after eight seconds without running runtime code', () => {
  const h = harness([{path: '/different/wechat.dylib'}]); h.load(); h.advance(8000);
  assert.equal(h.context.initialized, undefined);
  assert.equal(h.state.events.at(-1).code, 'core_module_timeout');
  assert.equal(h.state.events.at(-1).wait_ms, 8000);
  assert.equal(h.state.pending.length, 0);
});

test('ambiguous mappings fail immediately without waiting or running runtime code', () => {
  const h = harness([{path: CORE}, {path: CORE}]); h.load();
  assert.equal(h.context.initialized, undefined);
  assert.equal(h.state.events[0].code, 'core_module_ambiguous');
  assert.equal(h.state.pending.length, 0);
});

test('loaded UUID validation failure is returned immediately and never retried', () => {
  const h = harness([{path: CORE}]); h.context.badUuid = true; h.load();
  assert.equal(h.context.initialized, 1);
  assert.equal(h.state.events[0].code, 'runtime_validation_failed');
  assert.match(h.state.events[0].description, /UUID does not match/);
  assert.equal(h.state.pending.length, 0);
  assert.equal(h.context.rpc.exports, undefined);
});

test('a core arriving after a wait still runs the validation failure path once', () => {
  const h = harness([]); h.context.badUuid = true; h.load(); h.advance(100);
  h.state.modules = [{path: CORE}]; h.advance(200);
  assert.equal(h.context.initialized, 1);
  assert.equal(h.state.events.at(-1).code, 'runtime_validation_failed');
  assert.equal(h.state.events.at(-1).wait_ms, 200);
  assert.equal(h.state.pending.length, 0);
});
