// TLS front end for the installed Codex app-server. No external dependencies.
'use strict';
const fs = require('fs');
const path = require('path');
const https = require('https');
const net = require('net');
const crypto = require('crypto');
const { spawn } = require('child_process');
const { DesktopIPC } = require('./desktop-ipc');
const root = path.join(process.env.USERPROFILE, '.config', 'evo-codex-server');
const settings = JSON.parse(fs.readFileSync(path.join(root, 'settings.json'), 'utf8').replace(/^\uFEFF/, ''));
const token = fs.readFileSync(path.join(root, 'token'), 'utf8').trim();
const sockets = new Set();
let child, server, stopping = false;
const desktop = new DesktopIPC(fault => log('desktop-ipc ' + JSON.stringify(fault)), async threadId => {
  // DesktopIPC validates the exact stored thread UUID before invoking this.
  // Delegate loading to the GUI, preserving its settings and tools instead of
  // resuming the same conversation in our separate app-server process.
  const launcher = spawn(path.join(process.env.WINDIR, 'explorer.exe'), ['codex://threads/' + threadId],
    {windowsHide: true, stdio: 'ignore'});
  await new Promise((resolve, reject) => { launcher.once('spawn', resolve); launcher.once('error', reject); });
  launcher.unref();
});
let desktopRequests = 0;
function log(event) {
  const file = path.join(root, 'startup.log');
  if (fs.existsSync(file) && fs.statSync(file).size > 32768) fs.writeFileSync(file, '');
  fs.appendFileSync(file, new Date().toISOString() + ' ' + event + '\n');
}
function authorized(req) {
  const given = Buffer.from(req.headers.authorization || '');
  const expected = Buffer.from('Bearer ' + token);
  return given.length === expected.length && crypto.timingSafeEqual(given, expected);
}
function allowedPeer(req) {
  const ip = (req.socket.remoteAddress || '').replace(/^::ffff:/, '');
  return ['127.0.0.1', settings.evoIp, settings.pcIp].includes(ip);
}
function resolveCodex() {
  const base = path.join(process.env.LOCALAPPDATA, 'OpenAI', 'Codex', 'bin');
  if (fs.existsSync(base)) {
    const candidates = fs.readdirSync(base).map(x => path.join(base, x, 'codex.exe'))
      .filter(x => fs.existsSync(x)).sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs);
    if (candidates.length) return candidates[0];
  }
  if (fs.existsSync(settings.codex)) return settings.codex;
  throw new Error('Codex executable unavailable');
}
async function waitBackend() {
  for (let i = 0; i < 100; i++) {
    if (child.exitCode !== null) throw new Error('Codex exited before readiness');
    const ready = await new Promise(resolve => {
      const sock = net.connect(settings.backendPort, '127.0.0.1');
      sock.setTimeout(500);
      sock.once('connect', () => { sock.destroy(); resolve(true); });
      sock.once('error', () => { sock.destroy(); resolve(false); });
      sock.once('timeout', () => { sock.destroy(); resolve(false); });
    });
    if (ready) return;
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error('Codex listener did not become ready');
}
function stop(code) {
  if (stopping) return;
  stopping = true;
  desktop.close();
  for (const sock of sockets) sock.destroy();
  if (server) server.close();
  if (child && child.exitCode === null) child.kill();
  log('stopped code=' + code);
  process.exit(code);
}
async function desktopRequest(req, res) {
  if (req.headers.origin || !/^application\/json(?:;|$)/i.test(req.headers['content-type'] || '')) {
    res.writeHead(400); res.end(); return;
  }
  if (desktopRequests >= 2) { res.writeHead(429); res.end(); return; }
  desktopRequests++;
  res.setHeader('Content-Type', 'application/json');
  res.setHeader('Cache-Control', 'no-store');
  try {
    let size = 0;
    const chunks = [];
    for await (const chunk of req) {
      size += chunk.length;
      if (size > 131072) { res.writeHead(413); res.end(); return; }
      chunks.push(chunk);
    }
    let args;
    try { args = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
    catch (_) { res.writeHead(400); res.end(JSON.stringify({success: false, error: 'Invalid JSON'})); return; }
    if (!args || typeof args !== 'object' || Array.isArray(args)) {
      res.writeHead(400); res.end(); return;
    }
    const result = await desktop.action(args);
    res.end(JSON.stringify({success: true, ...result}));
  } catch (error) {
    if (!res.headersSent) res.writeHead(503);
    res.end(JSON.stringify({success: false, error_code: error.code || 'desktop_unavailable',
      error: error.code ? error.message.slice(0, 350) : 'Desktop IPC unavailable; open the Codex app and check status.'}));
  } finally { desktopRequests--; }
}
async function main() {
  if (!/^[A-Za-z0-9_-]{40,}$/.test(token)) throw new Error('Invalid capability token');
  child = spawn(resolveCodex(), ['app-server', '--listen', 'ws://127.0.0.1:' + settings.backendPort,
    '--ws-auth', 'capability-token', '--ws-token-file', path.join(root, 'token')],
    { cwd: settings.workspace, windowsHide: true, stdio: ['ignore', 'ignore', 'ignore'] });
  child.once('error', () => stop(1));
  child.once('exit', code => { if (!stopping) stop(code || 1); });
  await waitBackend();
  server = https.createServer({ pfx: fs.readFileSync(path.join(root, 'server.pfx')),
    passphrase: fs.readFileSync(path.join(root, 'pfx-password'), 'utf8').trim(),
    minVersion: 'TLSv1.2', requestTimeout: 15000, headersTimeout: 10000,
    maxHeaderSize: 8192 }, (req, res) => {
      if (!allowedPeer(req) || !authorized(req)) { res.writeHead(401); res.end(); return; }
      if (req.method === 'POST' && req.url === '/desktop') {
        desktopRequest(req, res).catch(() => { if (!res.destroyed) res.destroy(); }); return;
      }
      if (req.method !== 'GET' || req.url !== '/health') { res.writeHead(404); res.end(); return; }
      res.setHeader('Content-Type', 'application/json');
      res.setHeader('Cache-Control', 'no-store');
      res.end(JSON.stringify({ ready: child.exitCode === null, transport: 'codex-app-server', tls: true,
        desktop_ipc: true, desktop_connected: Boolean(desktop.clientId),
        desktop_last_error: desktop.lastFault }));
    });
  server.on('upgrade', (req, socket, head) => {
    if (!allowedPeer(req) || !authorized(req) || req.headers.origin || req.url !== '/') {
      socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n'); return;
    }
    if (sockets.size >= 16) { socket.destroy(); return; }
    const upstream = net.connect(settings.backendPort, '127.0.0.1');
    sockets.add(socket); sockets.add(upstream);
    const close = () => { socket.destroy(); upstream.destroy(); sockets.delete(socket); sockets.delete(upstream); };
    socket.on('error', close); upstream.on('error', close);
    socket.on('close', close); upstream.on('close', close);
    socket.setKeepAlive(true, 30000); upstream.setKeepAlive(true, 30000);
    upstream.once('connect', () => {
      // Pass the authenticated HTTP upgrade and then preserve WebSocket frames.
      const headers = req.rawHeaders.map((v, i) => i % 2 ? v + '\r\n' : v + ': ').join('');
      upstream.write('GET / HTTP/1.1\r\n' + headers + '\r\n');
      if (head.length) upstream.write(head);
      socket.pipe(upstream); upstream.pipe(socket);
    });
  });
  server.on('tlsClientError', () => {});
  server.once('error', () => stop(1));
  await new Promise(resolve => server.listen(settings.port, '0.0.0.0', resolve));
  fs.writeFileSync(path.join(root, 'state.json'), JSON.stringify({ pid: process.pid,
    codexPid: child.pid, port: settings.port, backendPort: settings.backendPort,
    startedAt: new Date().toISOString(), ready: true }));
  log('ready tls-port=' + settings.port + ' codex-pid=' + child.pid);
}
process.on('SIGTERM', () => stop(0));
process.on('SIGINT', () => stop(0));
process.on('uncaughtException', () => stop(1));
process.on('unhandledRejection', () => stop(1));
// Supervisor assigns a kill-on-close Job before allowing children to start.
process.stdin.once('data', () => main().catch(() => stop(1)));
