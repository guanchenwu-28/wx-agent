import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {execFileSync} from 'node:child_process';
import {setting, testApp, cache} from './runtime_config.mjs';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const SEND_ACTIONS = {send: 'text', 'send-image': 'image', 'send-voice': 'voice'};
const KINDS = Object.values(SEND_ACTIONS);
const STAT_FIELDS = ['dev', 'ino', 'size', 'mtime_ns', 'ctime_ns'];
export const MAX_FRAME_BYTES = 2 * 1024 * 1024;

export function buildRuntimeSource(descriptor, runtime) {
  return 'globalThis.REA_ADAPTER = ' + JSON.stringify(descriptor) + ';\n' + `
(function () {
 const started = Date.now();
 let announced = false;
 function failed(code, error) {
  send({kind:'rea_runtime_initialization_error',code,description:String(error),
   stack:typeof error.stack==='string'?error.stack:undefined,wait_ms:Date.now()-started});
 }
 function initialize() {
${runtime}
 }
 function check() {
  try {
   const modules = Process.enumerateModules().filter(module => module.path === globalThis.REA_ADAPTER.core_path);
   if (modules.length > 1) return failed('core_module_ambiguous','Multiple modules match adapter core_path');
   if (modules.length === 0) {
    if (!announced) {announced=true;send({kind:'rea_runtime_waiting'});}
    if (Date.now()-started >= 8000) return failed('core_module_timeout','WeChat core module did not load within 8 seconds');
    setTimeout(check,100);
    return;
   }
   initialize();
   send({kind:'rea_runtime_ready',exports:Object.keys(rpc.exports),wait_ms:Date.now()-started});
  } catch(error) {failed('runtime_validation_failed',error);}
 }
 check();
})();\n`;
}

export function fileStat(filename) {
  try {
    const value = fs.statSync(filename, {bigint: true});
    return {dev: String(value.dev), ino: String(value.ino), size: String(value.size),
      mtime_ns: String(value.mtimeNs), ctime_ns: String(value.ctimeNs)};
  } catch (error) {
    if (error.code === 'ENOENT' || error.code === 'ENOTDIR') return null;
    throw error;
  }
}

function sameStat(left, right) {
  if (left === null || right === null) return left === right;
  return !!left && !!right && STAT_FIELDS.every(key => left[key] === right[key]);
}

function sameProcess(left, right) {
  return !!left && !!right && ['pid', 'start', 'exe'].every(key => left[key] === right[key]);
}

function sameDependencies(dependencies) {
  if (!dependencies || !dependencies.files || !dependencies.profile_directory ||
      !Array.isArray(dependencies.profile_names)) return false;
  for (const [filename, expected] of Object.entries(dependencies.files)) {
    if (!sameStat(fileStat(filename), expected)) return false;
  }
  const names = fs.readdirSync(dependencies.profile_directory).filter(name => name.endsWith('.json')).sort();
  return JSON.stringify(names) === JSON.stringify(dependencies.profile_names);
}

function validateRequest(request) {
  if (!request || typeof request !== 'object' || Array.isArray(request)) throw Error('Runtime request must be an object');
  if (![...Object.keys(SEND_ACTIONS), 'status', 'probe'].includes(request.action)) throw Error('Unknown action');
  const construct = request.construct_only === undefined ? false : request.construct_only;
  if (typeof construct !== 'boolean' || (construct && request.action !== 'probe')) throw Error('Construction checks require action=probe');
  if (request.action in SEND_ACTIONS && (typeof request.request_id !== 'string' || !request.request_id.trim() || request.request_id.length > 160)) {
    throw Error('Native sends require a durable request_id');
  }
  if (request.text !== undefined && (typeof request.text !== 'string' || Buffer.byteLength(request.text, 'utf8') > 65536)) throw Error('Text exceeds the supported size');
  if (request.to !== undefined && (typeof request.to !== 'string' || request.to.length > 1024 || request.to.includes('\0'))) throw Error('Invalid recipient');
  // Bridge paths and in-memory payloads are always supplied by this host after
  // checking the corresponding source files, never accepted from a client.
  if ('bridge' in request || 'voice_bytes' in request) throw Error('Host-owned bridge/payload fields are not accepted');
}

function authorizationKey(request) {
  return JSON.stringify({construct: request.construct_only === true,
    test: request.adapter_test == null ? null : request.adapter_test,
    ...(request.adapter_test == null ? {} : {to: request.to, request_id: request.request_id})});
}

function validateAuthorization(report, request, descriptor) {
  const test = request.adapter_test;
  if (test != null) {
    if (!test || typeof test !== 'object' || Array.isArray(test) || !KINDS.includes(test.kind) ||
        request.to !== 'filehelper' || typeof request.request_id !== 'string' || !request.request_id.trim() ||
        test.request_id !== request.request_id || test.core_sha256 !== report.identity?.core_sha256) {
      throw Error('Adapter tests require the current core, matching request ID, and filehelper');
    }
    if (request.action in SEND_ACTIONS && SEND_ACTIONS[request.action] !== test.kind) throw Error('Adapter test kind does not match the requested send action');
  }
  if (!descriptor) {
    if (['status', 'probe'].includes(request.action) && request.construct_only !== true && test == null) return;
    throw Error('Version adaptation requires review before native calls');
  }
  if (request.action in SEND_ACTIONS && descriptor.permissions?.['send_' + SEND_ACTIONS[request.action]] !== true) {
    throw Error('This version has not passed an explicit filehelper test for ' + SEND_ACTIONS[request.action]);
  }
}

/** One session/script for one exact process and one permission set. No polling,
 * timers, sends or native calls occur unless execute() receives a request. */
export class TransportSession {
  constructor(options = {}) {
    this.root = options.root || ROOT;
    this.core = options.core || path.join(testApp, 'Contents/Resources/wechat.dylib');
    this.exe = options.exe || path.join(testApp, 'Contents/MacOS/WeChat');
    this.cache = options.cache || cache;
    this.python = options.python || setting('python', 'python3');
    this.sdk = options.sdk || setting('frida_module', path.join(ROOT, 'node_modules/frida/build/src/frida.js'));
    this.report = null;
    this.session = null;
    this.script = null;
    this.processIdentity = null;
    this.descriptorKey = null;
    this.authorizations = new Map();
    this.active = null;
    this.bridge = null;
    this.hostCode = Object.fromEntries(['transport.mjs', 'transport_session.mjs'].map(name =>
      [path.join(this.root, name), fileStat(path.join(this.root, name))]));
    this.stats = {requests: 0, cache_hits: 0, resolutions: 0, full_resolutions: 0,
      attaches: 0, script_loads: 0, identity_checks: 0, invalidations: 0, module_waits: 0};
  }

  snapshot(extra = {}) {
    return {...this.stats, broker_pid: process.pid, attached: !!this.session && !this.session.isDetached(), ...extra};
  }

  cancelPending() {
    // A pause received during asynchronous attach/load still stops this call
    // before the first native RPC. An RPC already dispatched is never replayed
    // or interrupted: its durable caller must receive the real/unknown result.
    if (this.active && !this.active.nativeDispatched) {
      this.active.cancelled = true;
      this.cancelInitialization?.();
    }
  }

  identifyProcess() {
    this.stats.identity_checks++;
    const text = execFileSync('/bin/ps', ['-axo', 'pid=,lstart=,comm='], {
      encoding: 'utf8', maxBuffer: 8 * 1024 * 1024, timeout: 10000,
      env: {...process.env, LC_ALL: 'C'}});
    const rows = text.split('\n').map(line => line.trim().match(/^(\d+)\s+(\S+\s+\S+\s+\d+\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+(.+)$/)).filter(Boolean);
    const matches = rows.filter(row => row[3] === this.exe);
    if (matches.length !== 1) throw Error('Start exactly one WeChat test copy first');
    return {pid: Number(matches[0][1]), start: matches[0][2].replace(/\s+/g, ' '), exe: matches[0][3]};
  }

  compatibility() {
    if (!this.report) return undefined;
    return Object.fromEntries(['status', 'profile_id', 'identity', 'capabilities', 'known_verified_image',
      'validation_scope', 'unresolved', 'report_path', 'cache_info'].map(key => [key, this.report[key]]));
  }

  async closeNative() {
    const script = this.script, session = this.session;
    this.script = null;
    this.session = null;
    this.descriptorKey = null;
    if (script) await script.unload().catch(() => {});
    if (session) await session.detach().catch(() => {});
  }

  async invalidate() {
    if (this.report || this.session) this.stats.invalidations++;
    await this.closeNative();
    this.report = null;
    this.authorizations.clear();
    this.bridge = null;
  }

  async resolve(request, identity) {
    let output;
    this.stats.resolutions++;
    try {
      output = execFileSync(this.python, [path.join(this.root, 'adapter.py'), '--core', this.core,
        '--descriptor', '--process-identity', JSON.stringify(identity), '--runtime-request', JSON.stringify(request)],
      {encoding: 'utf8', maxBuffer: 8 * 1024 * 1024, timeout: 60000});
    } catch (error) {
      throw Error('Version adaptation: ' + String(error.stdout || error.message).trim());
    }
    const report = JSON.parse(output);
    if (!report || typeof report !== 'object' || !report.identity || !report.core_stat || !report.dependency_stats) {
      throw Error('Version adaptation did not return file identity evidence');
    }
    if (report.cache_info?.core_hash_checked !== false) this.stats.full_resolutions++;
    if (!sameStat(fileStat(this.core), report.core_stat) || !sameDependencies(report.dependency_stats)) {
      throw Error('WeChat or adapter files changed during adaptation; scan again');
    }
    if (!sameProcess(report.process_identity, identity)) throw Error('Adapter process identity does not match');
    if (this.report && JSON.stringify(this.report.dependency_stats) !== JSON.stringify(report.dependency_stats)) {
      await this.closeNative();
      this.authorizations.clear();
      this.bridge = null;
    }
    this.report = report;
    this.authorizations.set(authorizationKey(request), report.runtime_descriptor ?? null);
    return report.runtime_descriptor ?? null;
  }

  async ensureScript(descriptor, identity) {
    const descriptorKey = JSON.stringify(descriptor);
    if (this.session && this.session.isDetached()) await this.closeNative();
    if (!this.session) {
      if (!sameStat(fileStat(this.core), this.report.core_stat)) throw Error('WeChat changed before attach; scan again');
      const frida = await import(pathToFileURL(this.sdk));
      this.session = await frida.attach(identity.pid);
      this.stats.attaches++;
      if (!sameProcess(this.identifyProcess(), identity)) throw Error('WeChat process changed while attaching');
    }
    if (this.script && this.descriptorKey === descriptorKey) return true;
    if (this.script) await this.script.unload();
    this.script = null;
    this.descriptorKey = null;
    const requiredExports = ['status', 'probe', 'sendText', 'sendImage', 'sendVoice'];
    // Frida load() can resolve after a top-level JS exception. A handshake at
    // the end of the runtime proves initialization reached the exports, without
    // invoking any native RPC or relying on the permissive exports Proxy.
    this.script = await this.session.createScript(buildRuntimeSource(descriptor, fs.readFileSync(path.join(this.root, 'runtime.js'), 'utf8')));
    let initialized = false, settleInitialization;
    const initialization = new Promise(resolve => {settleInitialization = resolve;});
    this.cancelInitialization = () => settleInitialization({error: {code: 'cancelled', description: 'Native transport paused before dispatch'}});
    this.script.message.connect(message => {
      const current = this.active;
      if (message.type === 'send' && message.payload?.kind === 'submitted' && current?.sendDispatched &&
          message.payload.request_id === current.request_id) current.submitted = true;
      if (!initialized && message.type === 'error') {
        settleInitialization({error: {description: String(message.description || 'Native runtime initialization failed').slice(0, 4000),
          file_name: message.fileName, line_number: message.lineNumber, column_number: message.columnNumber,
          stack: typeof message.stack === 'string' ? message.stack.slice(0, 4000) : undefined}});
      }
      if (!initialized && message.type === 'send' && message.payload?.kind === 'rea_runtime_ready') {
        settleInitialization({exports: message.payload.exports, wait_ms: message.payload.wait_ms || 0});
      }
      if (!initialized && message.type === 'send' && message.payload?.kind === 'rea_runtime_initialization_error') {
        const payload = message.payload;
        settleInitialization({error: {code: payload.code, description: String(payload.description).slice(0, 4000),
          stack: typeof payload.stack === 'string' ? payload.stack.slice(0, 4000) : undefined,
          wait_ms: payload.wait_ms}});
      }
      if (!initialized && message.type === 'send' && message.payload?.kind === 'rea_runtime_waiting') {
        this.stats.module_waits++;
      }
    });
    let timer, readiness;
    try {
      await this.script.load();
      this.stats.script_loads++;
      if (this.active?.cancelled) this.cancelInitialization();
      readiness = await Promise.race([initialization, new Promise(resolve => {
        timer = setTimeout(() => resolve({error: {description: 'Native runtime did not acknowledge initialization'}}), 10000);
      })]);
    } finally {
      clearTimeout(timer);
      this.cancelInitialization = null;
      initialized = true;
    }
    if (readiness.error || !Array.isArray(readiness.exports) || !requiredExports.every(name => readiness.exports.includes(name))) {
      const diagnostic = readiness.error || {description: 'Native runtime is missing required exports', exports: readiness.exports};
      const error = Error('Native runtime initialization failed: ' + diagnostic.description);
      error.runtime_initialization = diagnostic;
      throw error;
    }
    this.stats.last_module_wait_ms = readiness.wait_ms;
    this.descriptorKey = descriptorKey;
    return false;
  }

  preparePayload(request) {
    const prepared = {...request};
    if (request.action === 'send-image' || request.image_path !== undefined) {
      if (typeof request.image_path !== 'string' || !path.isAbsolute(request.image_path) || request.image_path.includes('\0')) throw Error('Image path must name an existing file');
      const stat = fs.statSync(request.image_path);
      if (!stat.isFile() || stat.size <= 0 || stat.size > 20 * 1024 * 1024) throw Error('Image path must name a nonempty <=20 MiB file');
    }
    if (request.action === 'send-voice' || request.voice_path !== undefined) {
      if (typeof request.voice_path !== 'string' || !path.isAbsolute(request.voice_path) || request.voice_path.includes('\0')) throw Error('Voice path must name a <=1 MiB SILK file');
      const stat = fs.statSync(request.voice_path);
      if (!stat.isFile() || stat.size <= 0 || stat.size > 1048576) throw Error('Voice path must name a <=1 MiB SILK file');
      prepared.voice_bytes = Array.from(fs.readFileSync(request.voice_path));
      if (prepared.voice_bytes.length !== stat.size || prepared.voice_bytes.length > 1048576) throw Error('Voice file changed while reading');
    }
    return prepared;
  }

  getBridge() {
    if (this.bridge && fs.existsSync(this.bridge)) return this.bridge;
    const source = path.join(this.root, 'native_message_bridge.cpp');
    const digest = crypto.createHash('sha256').update(fs.readFileSync(source)).digest('hex').slice(0, 16);
    fs.mkdirSync(this.cache, {recursive: true, mode: 0o700});
    const bridge = path.join(this.cache, 'native_message_bridge_' + digest + '.dylib');
    if (!fs.existsSync(bridge)) {
      const temporary = bridge + '.' + process.pid + '.tmp';
      try {
        execFileSync(setting('clang', 'clang++'), ['-std=c++17', '-dynamiclib', '-O2', '-fvisibility=hidden', source, '-o', temporary],
          {timeout: 30000, maxBuffer: 2 * 1024 * 1024});
        fs.renameSync(temporary, bridge);
      } finally {
        if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
      }
    }
    this.bridge = bridge;
    return bridge;
  }

  async execute(request) {
    this.stats.requests++;
    const current = {request_id: request?.request_id, submitted: false, sendDispatched: false,
      nativeDispatched: false, cancelled: false};
    if (this.active) return {error: 'Transport requests must be serialized', state: 'failed', submitted: false, transport: this.snapshot()};
    this.active = current;
    let cacheHit = false, scriptReused = false, didResolve = false;
    try {
      if (Object.entries(this.hostCode).some(([filename, stat]) => !sameStat(fileStat(filename), stat))) {
        const error = Error('Native transport code changed; restart required before another request');
        error.restart_required = true;
        throw error;
      }
      validateRequest(request);
      const prepared = this.preparePayload(request);
      const identity = this.identifyProcess();
      const processChanged = !sameProcess(identity, this.processIdentity);
      const detached = this.session && this.session.isDetached();
      if (processChanged || detached || (this.report && !sameDependencies(this.report.dependency_stats))) await this.invalidate();
      this.processIdentity = identity;
      const key = authorizationKey(request);
      let descriptor;
      if (this.report && this.authorizations.has(key)) {
        descriptor = this.authorizations.get(key);
        cacheHit = true;
        this.stats.cache_hits++;
      } else {
        didResolve = true;
        descriptor = await this.resolve(request, identity);
      }
      validateAuthorization(this.report, request, descriptor);
      if (!descriptor) return {adapter: this.compatibility(), logged_in: null, passive: true,
        submitted: false, native_available: false, transport: this.snapshot({cache_hit: cacheHit, script_reused: false,
          did_resolve: didResolve, core_hash_checked: didResolve && this.report.cache_info?.core_hash_checked !== false})};
      scriptReused = await this.ensureScript(descriptor, identity);
      if (current.cancelled) throw Error('Native transport paused before dispatch');
      if (request.action !== 'status' && (request.action !== 'probe' || request.construct_only === true)) prepared.bridge = this.getBridge();
      // Read metadata again immediately before the native call. A large-file
      // digest is unnecessary because this exact stat set was hashed by Python.
      if (!sameStat(fileStat(this.core), this.report.core_stat)) throw Error('WeChat changed before native call; scan again');
      let result;
      current.nativeDispatched = true;
      if (request.action === 'status') result = await this.script.exports.status();
      else if (request.action === 'probe') result = await this.script.exports.probe(prepared);
      else {
        current.sendDispatched = true;
        const method = {'send': 'sendText', 'send-image': 'sendImage', 'send-voice': 'sendVoice'}[request.action];
        result = await this.script.exports[method](prepared);
        current.submitted = current.submitted || result?.submitted === true;
        if (current.submitted) await new Promise(resolve => setTimeout(resolve, 500));
      }
      const attestation = request.adapter_test ? {adapter_test: {kind: request.adapter_test.kind,
        core_sha256: this.report.identity.core_sha256, recipient: 'filehelper', request_id: request.request_id}} : {};
      return {...result, ...attestation, adapter: this.compatibility(), submitted: current.submitted,
        process_alive: !this.session.isDetached(), pid: identity.pid,
        transport: this.snapshot({cache_hit: cacheHit, script_reused: scriptReused, did_resolve: didResolve,
          core_hash_checked: didResolve && this.report.cache_info?.core_hash_checked !== false})};
    } catch (error) {
      const compatibility = this.compatibility();
      await this.closeNative();
      return {error: String(error), state: current.submitted || current.sendDispatched ? 'unknown' : 'failed',
        submitted: current.submitted, ...(compatibility ? {adapter: compatibility} : {}),
        ...(error.restart_required ? {restart_required: true} : {}),
        ...(error.runtime_initialization ? {runtime_initialization: error.runtime_initialization} : {}),
        transport: this.snapshot({cache_hit: cacheHit, script_reused: scriptReused, did_resolve: didResolve,
          core_hash_checked: didResolve && this.report?.cache_info?.core_hash_checked === true})};
    } finally {
      this.active = null;
    }
  }

  async close() {
    await this.invalidate();
  }
}
