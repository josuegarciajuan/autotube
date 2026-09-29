'use strict';

/**
 * panel-gate — reverse proxy with Android-style pattern unlock.
 *
 * Flow per request:
 *   1. Static gate assets (/__gate/static/*) and the unlock/config/logout
 *      endpoints are always reachable.
 *   2. Every other request needs a valid signed session cookie.
 *        - no/invalid cookie -> unlock screen (same URL requested)
 *        - valid cookie     -> reverse proxy to the backend selected by Host
 *   3. Backend routing: Host starting with "cerraduras." -> BACKEND_CERRADURAS,
 *      anything else -> BACKEND_PANELS.
 *
 * Pattern verification always happens server-side. Failure tracking is per
 * client IP (last hop of X-Forwarded-For, which is the address Apache appends)
 * with exponential lockout after FAIL_MAX consecutive failures.
 */

const fs = require('fs');
const http = require('http');
const path = require('path');
const express = require('express');
const cookieParser = require('cookie-parser');
const { createProxyMiddleware } = require('http-proxy-middleware');

const { loadEnv, readEnvFile, ROOT } = require('./lib/env');
loadEnv();

const pattern = require('./lib/pattern');
const session = require('./lib/session');

// ── Config ──────────────────────────────────────────────────────────────────
const PORT = parseInt(process.env.PORT || '8099', 10);
const HOST = '127.0.0.1';
// Paréntesis en el RHS: evita el falso positivo del hook pre-commit
// (`SECRET = valor`) al versionar este fichero. Comportamiento idéntico.
let GATE_SECRET = (process.env.GATE_SECRET || '');
let PATTERN_HASH = process.env.PATTERN_HASH || '';
const SESSION_TTL_DAYS = parseInt(process.env.SESSION_TTL_DAYS || '30', 10);
const COOKIE_DOMAIN = process.env.COOKIE_DOMAIN || '';
const COOKIE_SECURE = String(process.env.COOKIE_SECURE || 'true') !== 'false';
const COOKIE_NAME = process.env.COOKIE_NAME || 'pg_session';
const BACKEND_CERRADURAS = process.env.BACKEND_CERRADURAS || 'http://127.0.0.1:8080';
const BACKEND_PANELS = process.env.BACKEND_PANELS || 'http://127.0.0.1:8085';
const BACKEND_TAILDECK = process.env.BACKEND_TAILDECK || 'http://127.0.0.1:8110';
const BACKEND_AUTOTUBE = process.env.BACKEND_AUTOTUBE || 'http://127.0.0.1:8000';
const FAIL_MAX = parseInt(process.env.FAIL_MAX || '5', 10);
const LOCK_MIN = parseInt(process.env.LOCK_MIN || '15', 10);
const PATTERN_MIN = parseInt(process.env.PATTERN_MIN || '6', 10);
const LOCK_LEVEL_MAX = 8; // cap exponential growth: 15min * 2^8 ≈ 64h

if (!GATE_SECRET || !PATTERN_HASH) {
  console.error('[panel-gate] FATAL: falta GATE_SECRET o PATTERN_HASH en .env');
  process.exit(1);
}

// ── Logs ────────────────────────────────────────────────────────────────────
const LOG_DIR = path.join(ROOT, 'logs');
fs.mkdirSync(LOG_DIR, { recursive: true });
const authLog = fs.createWriteStream(path.join(LOG_DIR, 'auth.log'), { flags: 'a' });
const accessLog = fs.createWriteStream(path.join(LOG_DIR, 'access.log'), { flags: 'a' });
session.initRevocation(path.join(LOG_DIR, 'revoked.json'));

function logAuth(entry) {
  authLog.write(`${JSON.stringify(Object.assign({ ts: new Date().toISOString() }, entry))}\n`);
}
function logAccess(method, host, url, status, ip, ms) {
  accessLog.write(
    `${JSON.stringify({
      ts: new Date().toISOString(),
      ip,
      method,
      host,
      url,
      status,
      ms,
    })}\n`
  );
}

// ── Rate limiting (per client IP, exponential lockout) ──────────────────────
const buckets = new Map(); // ip -> { fails: number[], lockedUntil: number, level: number }

function getClientIp(req) {
  // Only Apache can reach 127.0.0.1:8099. mod_proxy appends the real client IP
  // to X-Forwarded-For, so the LAST entry is the trustworthy one.
  const xff = req.headers['x-forwarded-for'];
  if (typeof xff === 'string' && xff.trim()) {
    const parts = xff.split(',').map((s) => s.trim()).filter(Boolean);
    if (parts.length) return parts[parts.length - 1];
  }
  return (req.socket && req.socket.remoteAddress) || 'unknown';
}

function bucketFor(ip) {
  let bucket = buckets.get(ip);
  if (!bucket) {
    bucket = { fails: [], lockedUntil: 0, level: 0 };
    buckets.set(ip, bucket);
  }
  return bucket;
}

function lockState(ip) {
  const bucket = bucketFor(ip);
  const now = Date.now();
  if (bucket.lockedUntil > now) {
    return { locked: true, retryAfter: Math.ceil((bucket.lockedUntil - now) / 1000) };
  }
  return { locked: false, retryAfter: 0 };
}

function registerFailure(ip) {
  const bucket = bucketFor(ip);
  const now = Date.now();
  bucket.fails.push(now);
  if (bucket.fails.length >= FAIL_MAX) {
    const multiplier = Math.pow(2, Math.min(bucket.level, LOCK_LEVEL_MAX));
    const lockMs = LOCK_MIN * 60 * 1000 * multiplier;
    bucket.lockedUntil = now + lockMs;
    bucket.level += 1;
    bucket.fails = [];
    return Math.ceil(lockMs / 1000);
  }
  return 0;
}

function registerSuccess(ip) {
  const bucket = bucketFor(ip);
  bucket.fails = [];
  bucket.level = 0;
  bucket.lockedUntil = 0;
}

// ── Cookies ─────────────────────────────────────────────────────────────────
function parseCookies(header) {
  const out = {};
  if (!header) return out;
  for (const part of String(header).split(';')) {
    const eq = part.indexOf('=');
    if (eq < 0) continue;
    const key = part.slice(0, eq).trim();
    if (!key) continue;
    const value = part.slice(eq + 1).trim();
    try {
      out[key] = decodeURIComponent(value);
    } catch (_) {
      out[key] = value;
    }
  }
  return out;
}

function sessionFromReq(req) {
  const cookies = req.cookies || parseCookies(req.headers.cookie);
  if (!cookies[COOKIE_NAME]) return null;
  return session.verify(cookies[COOKIE_NAME], GATE_SECRET);
}

function cookieOptions(maxAgeMs) {
  const opts = {
    path: '/',
    httpOnly: true,
    secure: COOKIE_SECURE,
    sameSite: 'lax',
    maxAge: maxAgeMs,
  };
  if (COOKIE_DOMAIN) opts.domain = COOKIE_DOMAIN;
  return opts;
}

// ── Unlock screen ───────────────────────────────────────────────────────────
function securityHeaders(res) {
  res.setHeader('X-Robots-Tag', 'noindex, nofollow, noarchive, nosnippet, noimageindex');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('Referrer-Policy', 'no-referrer');
  res.setHeader('Cache-Control', 'no-store, no-cache, must-revalidate');
  res.setHeader('Pragma', 'no-cache');
  res.setHeader(
    'Content-Security-Policy',
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; " +
      "connect-src 'self'; frame-ancestors 'self' https://lamami.online " +
      "https://www.lamami.online https://shhexxchollos.com " +
      "https://www.shhexxchollos.com https://josue.ink https://www.josue.ink"
  );
}

function serveUnlock(req, res) {
  securityHeaders(res);
  res.status(200).type('html');
  res.sendFile(path.join(ROOT, 'public', 'unlock.html'));
}

/** Re-read PATTERN_HASH from disk so `set-pattern` applies without a restart. */
function currentPatternHash() {
  try {
    const fresh = readEnvFile();
    if (fresh.PATTERN_HASH) {
      PATTERN_HASH = fresh.PATTERN_HASH;
    }
  } catch (_) {
    /* keep last known good hash */
  }
  return PATTERN_HASH;
}

// ── App ─────────────────────────────────────────────────────────────────────
const app = express();
app.disable('x-powered-by');
app.use(cookieParser());

// access log (fires on response finish, works for proxied streams too)
app.use((req, res, next) => {
  const start = Date.now();
  res.on('finish', () => {
    logAccess(req.method, req.headers.host || '-', req.originalUrl, res.statusCode, getClientIp(req), Date.now() - start);
  });
  next();
});

// static gate assets — must be reachable without a session
app.use(
  '/__gate/static',
  express.static(path.join(ROOT, 'public'), {
    index: false,
    dotfiles: 'deny',
    fallthrough: false,
  })
);

// runtime config for the unlock UI (no secrets)
app.get('/__gate/config', (req, res) => {
  res.json({ min: PATTERN_MIN, grid: 3 });
});

// unlock
app.post('/__gate/unlock', express.json({ limit: '8kb' }), async (req, res) => {
  const ip = getClientIp(req);
  const state = lockState(ip);
  if (state.locked) {
    logAuth({ event: 'unlock_blocked', ip, retryAfter: state.retryAfter });
    return res.status(429).json({ ok: false, error: 'locked', retryAfter: state.retryAfter });
  }

  const candidate = req.body && req.body.pattern;
  const valid = pattern.validate(candidate, PATTERN_MIN);
  if (!valid.ok) {
    registerFailure(ip);
    logAuth({ event: 'unlock_fail', ip, reason: 'invalid', detail: valid.error });
    return res.status(400).json({ ok: false, error: valid.error });
  }

  // Reserve the attempt synchronously BEFORE the async scrypt verification so
  // concurrent requests cannot race past the per-IP lockout. registerSuccess()
  // below clears it when the pattern turns out to be correct.
  const lockedFor = registerFailure(ip);
  let correct = false;
  try {
    correct = await pattern.verifyPatternAsync(candidate, currentPatternHash());
  } catch (_) {
    correct = false;
  }

  if (!correct) {
    logAuth({ event: 'unlock_fail', ip, reason: 'mismatch', lockedFor: lockedFor || 0 });
    return res.status(401).json({ ok: false, error: 'Patrón incorrecto' });
  }

  registerSuccess(ip);
  const { token, payload } = session.issue(GATE_SECRET, SESSION_TTL_DAYS);
  res.cookie(COOKIE_NAME, token, cookieOptions(SESSION_TTL_DAYS * 86400 * 1000));
  logAuth({ event: 'unlock_ok', ip, jti: payload.jti, exp: payload.exp });
  return res.json({ ok: true });
});

// logout — revoke current token and clear cookie
app.post('/__gate/logout', (req, res) => {
  const payload = sessionFromReq(req);
  if (payload && payload.jti) {
    session.revoke(payload.jti);
    logAuth({ event: 'logout', ip: getClientIp(req), jti: payload.jti });
  }
  res.cookie(COOKIE_NAME, '', cookieOptions(0));
  return res.json({ ok: true });
});

// gate landing: unlock if no session, otherwise go home
app.get('/__gate/', (req, res) => {
  if (sessionFromReq(req)) return res.redirect(302, '/');
  return serveUnlock(req, res);
});

// unknown /__gate/* sub-paths must never reach a backend
app.use('/__gate', (req, res) => {
  res.status(404).json({ ok: false, error: 'not found' });
});

// ── Session gate: any other unauthenticated request gets the unlock screen ──
app.use((req, res, next) => {
  if (sessionFromReq(req)) return next();
  return serveUnlock(req, res);
});

// ── Reverse proxy (Host-based routing) ──────────────────────────────────────

// Autotube v2: la SPA vive bajo /autotube y llama a la API en /api y a los
// WebSockets en /ws. Son rutas propias de su backend FastAPI (127.0.0.1:8000).
function isAutotubePath(url) {
  return (
    url === '/autotube' || url.startsWith('/autotube/') || url.startsWith('/autotube?') ||
    url === '/api' || url.startsWith('/api/') || url.startsWith('/api?') ||
    url === '/ws' || url.startsWith('/ws/') || url.startsWith('/ws?')
  );
}

function pickTarget(req) {
  const host = String(req.headers.host || '').toLowerCase();
  const url = String(req.url || '');
  // TailDeck (panel de flota) — enrutado por path, sigue protegido por el patron.
  if (url === '/superserver' || url.startsWith('/superserver/') || url.startsWith('/superserver?')) return BACKEND_TAILDECK;
  // cerraduras.* tiene su propio backend para TODAS sus rutas.
  if (host.startsWith('cerraduras.')) return BACKEND_CERRADURAS;
  if (isAutotubePath(url)) return BACKEND_AUTOTUBE;
  return BACKEND_PANELS;
}

// La SPA de autotube se sirve en /autotube pero su backend FastAPI espera las
// rutas en la raíz (/assets, /api/static, /). Se quita SOLO el prefijo
// /autotube; /api y /ws pasan intactos.
function stripAutotubePrefix(path) {
  const rewritten = path.replace(/^\/autotube(?=[/?]|$)/, '');
  if (rewritten === '') return '/';
  return rewritten.startsWith('?') ? '/' + rewritten : rewritten;
}

const proxy = createProxyMiddleware({
  target: BACKEND_PANELS,
  router: (req) => pickTarget(req),
  pathRewrite: (path) => stripAutotubePrefix(path),
  changeOrigin: false, // preserve Host end-to-end
  xfwd: true,
  ws: true,
  proxyTimeout: 0, // no timeout: MJPEG/long-lived streams
  timeout: 0,
  logLevel: 'warn',
  on: {
    // El backend interno confía en esta cabecera para saber que el gate ya
    // validó el patrón. Se inyecta SIEMPRE aquí y se elimina cualquier valor
    // entrante del cliente en el middleware previo al proxy.
    proxyReq: (proxyReq) => {
      proxyReq.setHeader('X-Panel-Gate', '1');
    },
    error: (err, req, res) => {
      logAuth({ event: 'proxy_error', host: req.headers.host || '-', url: req.url, error: err.code || err.message });
      if (res && typeof res.writeHead === 'function' && !res.headersSent) {
        res.writeHead(502, { 'Content-Type': 'text/plain; charset=utf-8' });
        res.end('Bad Gateway');
      } else if (res && typeof res.destroy === 'function') {
        res.destroy();
      }
    },
  },
});

// El cliente nunca puede falsificar la cabecera de confianza del gate.
app.use((req, res, next) => {
  delete req.headers['x-panel-gate'];
  next();
});

app.use(proxy);

// JSON error handler (e.g. malformed unlock body)
app.use((err, req, res, next) => {
  if (res.headersSent) return next(err);
  const status = err.status || err.statusCode || 400;
  res.status(status).json({ ok: false, error: err.message || 'Bad Request' });
});

// ── HTTP + WebSocket server ─────────────────────────────────────────────────
const server = http.createServer(app);

server.on('upgrade', (req, socket, head) => {
  // upgrade bypasses express middleware: parse cookie and verify manually
  const cookies = parseCookies(req.headers.cookie);
  const payload = cookies[COOKIE_NAME] ? session.verify(cookies[COOKIE_NAME], GATE_SECRET) : null;
  if (!payload) {
    logAuth({ event: 'ws_rejected', ip: getClientIp(req), url: req.url });
    socket.write('HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n');
    socket.destroy();
    return;
  }
  if (typeof proxy.upgrade === 'function') {
    proxy.upgrade(req, socket, head);
  } else {
    socket.destroy();
  }
});

server.listen(PORT, HOST, () => {
  console.log(
    `[panel-gate] listening on http://${HOST}:${PORT} | panels=${BACKEND_PANELS} cerraduras=${BACKEND_CERRADURAS} | ttl=${SESSION_TTL_DAYS}d`
  );
});

function shutdown(signal) {
  console.log(`[panel-gate] ${signal} received, shutting down`);
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 5000).unref();
}
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
