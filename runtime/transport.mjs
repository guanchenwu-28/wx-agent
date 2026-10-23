import fs from 'node:fs';
import path from 'node:path';
import net from 'node:net';
import {pathToFileURL} from 'node:url';
import {TransportSession, MAX_FRAME_BYTES} from './transport_session.mjs';

function failure(message) {
  return {error: message, state: 'failed', submitted: false};
}

async function oneShot() {
  const native = new TransportSession();
  const chunks = [];
  let bytes = 0;
  try {
    for await (const chunk of process.stdin) {
      bytes += chunk.length;
      if (bytes > MAX_FRAME_BYTES) throw Error('Transport request exceeds 2 MiB');
      chunks.push(chunk);
    }
    const result = await native.execute(JSON.parse(Buffer.concat(chunks).toString('utf8')));
    console.log(JSON.stringify(result));
    if (result.error) process.exitCode = 1;
  } catch (error) {
    console.log(JSON.stringify(failure(String(error))));
    process.exitCode = 1;
  } finally {
    await native.close();
  }
}

export async function serve({socketPath, idleMs = 60000, native = new TransportSession()}) {
  if (!path.isAbsolute(socketPath) || socketPath.includes('\0')) throw Error('An absolute socket path is required');
  const parent = fs.lstatSync(path.dirname(socketPath));
  if (!parent.isDirectory() || parent.isSymbolicLink() || parent.uid !== process.getuid() || (parent.mode & 0o777) !== 0o700) {
    throw Error('Transport socket parent must be an owned private 0700 directory');
  }
  if (fs.existsSync(socketPath)) throw Error('Transport socket already exists');
  const sockets = new Set();
  let queue = [], active = false, stopping = false, closing = false, timer, socketIdentity;
  const stopWaiters = [];
  const reply = (socket, value) => {
    if (!socket.destroyed) socket.end(JSON.stringify(value) + '\n');
  };
  function armIdle() {
    clearTimeout(timer);
    if (!stopping && !active && queue.length === 0) timer = setTimeout(() => stop(), idleMs);
  }
  async function finishStop() {
    if (closing || active) return;
    closing = true;
    await native.close();
    server.close();
    const result = {ok: true, stopped: true, running: false, pid: process.pid, transport: native.snapshot()};
    for (const socket of stopWaiters.splice(0)) reply(socket, result);
    for (const socket of sockets) if (!socket.writableEnded) reply(socket, failure('Native transport is stopping'));
    // Node unlinks its Unix listener on close. Only remove a leftover entry if
    // it is still the exact socket we created; never unlink a replacement.
    try {
      const stat = fs.lstatSync(socketPath);
      if (socketIdentity && stat.isSocket() && stat.dev === socketIdentity.dev && stat.ino === socketIdentity.ino) fs.unlinkSync(socketPath);
    } catch (error) {
      if (error.code !== 'ENOENT') process.exitCode = 1;
    }
  }
  function stop(waiter) {
    if (waiter) stopWaiters.push(waiter);
    if (!stopping) {
      stopping = true;
      clearTimeout(timer);
      native.cancelPending?.();
      for (const pending of queue.splice(0)) reply(pending.socket, failure('Native transport paused before dispatch'));
    }
    void finishStop();
  }
  async function drain() {
    if (active || stopping) return;
    const pending = queue.shift();
    if (!pending) return armIdle();
    if (pending.socket.destroyed) return void drain();
    active = true;
    clearTimeout(timer);
    let result;
    try { result = await native.execute(pending.request); }
    catch (error) {
      result = {...failure(String(error)),
        ...(['send', 'send-image', 'send-voice'].includes(pending.request.action) ? {state: 'unknown'} : {})};
    }
    reply(pending.socket, result);
    active = false;
    if (result.restart_required) stop();
    if (stopping) await finishStop();
    else void drain();
  }
  const server = net.createServer(socket => {
    sockets.add(socket);
    socket.on('error', () => {});
    socket.on('close', () => sockets.delete(socket));
    if (stopping || sockets.size > 64) return reply(socket, failure('Native transport is stopping or busy'));
    let chunks = [], bytes = 0, framed = false;
    socket.setTimeout(10000, () => socket.destroy());
    socket.on('data', chunk => {
      if (framed) return socket.destroy();
      bytes += chunk.length;
      if (bytes > MAX_FRAME_BYTES) {framed = true; return reply(socket, failure('Transport request exceeds 2 MiB'));}
      chunks.push(chunk);
      const input = Buffer.concat(chunks);
      const newline = input.indexOf(10);
      if (newline < 0) return;
      framed = true;
      socket.setTimeout(0);
      chunks = [];
      if (newline !== input.length - 1) return reply(socket, failure('Exactly one NDJSON request per connection is required'));
      let request;
      try {request = JSON.parse(input.subarray(0, newline).toString('utf8'));}
      catch {return reply(socket, failure('Invalid transport JSON'));}
      if (!request || typeof request !== 'object' || Array.isArray(request)) return reply(socket, failure('Transport request must be an object'));
      if (request.action === '_transport_stop') return stop(socket);
      if (stopping) return reply(socket, failure('Native transport is stopping'));
      if (request.action === '_transport_health') {
        reply(socket, {ok: true, running: true, pid: process.pid, transport: native.snapshot()});
        return armIdle();
      }
      if (queue.length >= 32) return reply(socket, failure('Native transport request queue is full'));
      queue.push({socket, request});
      void drain();
    });
    socket.on('end', () => {if (!framed) socket.end();});
  });
  await new Promise((resolve, reject) => {
    // Unix socket permissions are restrictive from the instant of bind.
    const oldUmask = process.umask(0o177);
    const failed = error => {process.umask(oldUmask); reject(error);};
    server.once('error', failed);
    try {
      server.listen(socketPath, () => {
        process.umask(oldUmask);
        server.off('error', failed);
        resolve();
      });
    } catch (error) {
      server.off('error', failed);
      failed(error);
    }
  });
  socketIdentity = fs.lstatSync(socketPath);
  if ((socketIdentity.mode & 0o777) !== 0o600) throw Error('Transport socket is not private');
  server.on('error', () => {process.exitCode = 1; stop();});
  process.once('SIGTERM', () => stop());
  process.once('SIGINT', () => stop());
  console.log(JSON.stringify({ready: true, pid: process.pid, socket: socketPath}));
  armIdle();
  return {stop, server};
}

async function main() {
  try {
    const args = process.argv.slice(2);
    if (args.length === 0) await oneShot();
    else {
      if (args[0] !== '--serve') throw Error('Expected --serve');
      let socketPath, idleMs = 60000;
      for (let index = 1; index < args.length; index += 2) {
        if (args[index] === '--socket' && args[index + 1]) socketPath = args[index + 1];
        else if (args[index] === '--idle-ms' && args[index + 1] && /^\d+$/.test(args[index + 1])) idleMs = Number(args[index + 1]);
        else throw Error('Invalid transport server option');
      }
      if (!socketPath || !Number.isSafeInteger(idleMs) || idleMs < 100 || idleMs > 300000) throw Error('Invalid socket or idle timeout');
      await serve({socketPath, idleMs});
    }
  } catch (error) {
    console.log(JSON.stringify(failure(String(error))));
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) await main();
