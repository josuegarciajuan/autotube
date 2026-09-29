'use strict';

/**
 * lib/pattern.js — scrypt hash/verify for the unlock pattern.
 *
 * The pattern is NEVER stored in clear text. Only a salted scrypt digest is
 * persisted (PATTERN_HASH in .env). Comparison uses crypto.timingSafeEqual.
 *
 * Stored format (single line, shell-safe):
 *   scrypt$<N>$<r>$<p>$<saltBase64url>$<hashBase64url>
 */

const crypto = require('crypto');

const DEFAULT_PARAMS = Object.freeze({ N: 16384, r: 8, p: 1, keylen: 64 });
const MIN_POINTS = 6;

/** Parse "0-1-2-5-8" into [0,1,2,5,8] or return null if malformed. */
function normalize(pattern) {
  if (typeof pattern !== 'string') return null;
  const raw = pattern.trim();
  if (!raw) return null;
  const parts = raw.split('-');
  const nodes = [];
  for (const part of parts) {
    if (!/^\d+$/.test(part)) return null;
    const node = Number(part);
    if (!Number.isInteger(node) || node < 0 || node > 8) return null;
    nodes.push(node);
  }
  return nodes;
}

/** Validate structure: 6+ nodes, all distinct, grid 0..8. */
function validate(pattern, minPoints) {
  const min = Number.isInteger(minPoints) ? minPoints : MIN_POINTS;
  const nodes = normalize(pattern);
  if (!nodes) return { ok: false, error: 'Formato de patrón inválido' };
  if (nodes.length < min) {
    return { ok: false, error: `El patrón debe tener al menos ${min} puntos` };
  }
  if (new Set(nodes).size !== nodes.length) {
    return { ok: false, error: 'El patrón no puede repetir puntos' };
  }
  return { ok: true, grid: nodes };
}

/** Hash a normalized "n-n-n" pattern string with a fresh random salt. */
function hashPattern(pattern) {
  const nodes = normalize(pattern);
  if (!nodes) throw new Error('Patrón inválido para hashear');
  const canonical = nodes.join('-');
  const salt = crypto.randomBytes(16);
  const hash = crypto.scryptSync(canonical, salt, DEFAULT_PARAMS.keylen, {
    N: DEFAULT_PARAMS.N,
    r: DEFAULT_PARAMS.r,
    p: DEFAULT_PARAMS.p,
    maxmem: 64 * 1024 * 1024,
  });
  return [
    'scrypt',
    DEFAULT_PARAMS.N,
    DEFAULT_PARAMS.r,
    DEFAULT_PARAMS.p,
    salt.toString('base64url'),
    hash.toString('base64url'),
  ].join('$');
}

/** Constant-time verify of a candidate pattern against the stored digest. */
function verifyPattern(pattern, stored) {
  if (typeof pattern !== 'string' || typeof stored !== 'string') return false;
  const nodes = normalize(pattern);
  if (!nodes) return false;
  const parts = stored.split('$');
  if (parts.length !== 6 || parts[0] !== 'scrypt') return false;

  const N = Number(parts[1]);
  const r = Number(parts[2]);
  const p = Number(parts[3]);
  if (!Number.isInteger(N) || !Number.isInteger(r) || !Number.isInteger(p)) return false;
  if (N < 1024 || N > 1 << 20 || r < 1 || r > 32 || p < 1 || p > 16) return false;

  let salt;
  let expected;
  try {
    salt = Buffer.from(parts[4], 'base64url');
    expected = Buffer.from(parts[5], 'base64url');
  } catch (_) {
    return false;
  }
  if (!salt.length || !expected.length) return false;

  let actual;
  try {
    actual = crypto.scryptSync(nodes.join('-'), salt, expected.length, {
      N,
      r,
      p,
      maxmem: 128 * 1024 * 1024,
    });
  } catch (_) {
    return false;
  }
  if (actual.length !== expected.length) return false;
  return crypto.timingSafeEqual(actual, expected);
}

/** Promisified crypto.scrypt (runs on the libuv threadpool, off the event loop). */
function scryptAsync(password, salt, keylen, options) {
  return new Promise((resolve, reject) => {
    crypto.scrypt(password, salt, keylen, options, (err, derivedKey) => {
      if (err) reject(err);
      else resolve(derivedKey);
    });
  });
}

/**
 * Async counterpart of verifyPattern. Identical parsing/validation and the same
 * constant-time comparison, but scrypt is computed off the main thread so a
 * flood of unlock attempts cannot stall every other request on the gate.
 */
async function verifyPatternAsync(pattern, stored) {
  if (typeof pattern !== 'string' || typeof stored !== 'string') return false;
  const nodes = normalize(pattern);
  if (!nodes) return false;
  const parts = stored.split('$');
  if (parts.length !== 6 || parts[0] !== 'scrypt') return false;

  const N = Number(parts[1]);
  const r = Number(parts[2]);
  const p = Number(parts[3]);
  if (!Number.isInteger(N) || !Number.isInteger(r) || !Number.isInteger(p)) return false;
  if (N < 1024 || N > 1 << 20 || r < 1 || r > 32 || p < 1 || p > 16) return false;

  let salt;
  let expected;
  try {
    salt = Buffer.from(parts[4], 'base64url');
    expected = Buffer.from(parts[5], 'base64url');
  } catch (_) {
    return false;
  }
  if (!salt.length || !expected.length) return false;

  let actual;
  try {
    actual = await scryptAsync(nodes.join('-'), salt, expected.length, {
      N,
      r,
      p,
      maxmem: 128 * 1024 * 1024,
    });
  } catch (_) {
    return false;
  }
  if (actual.length !== expected.length) return false;
  return crypto.timingSafeEqual(actual, expected);
}

module.exports = { normalize, validate, hashPattern, verifyPattern, verifyPatternAsync, MIN_POINTS };
