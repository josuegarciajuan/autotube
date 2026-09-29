'use strict';

/**
 * lib/session.js — signed session tokens (HMAC-SHA256) + simple revocation list.
 *
 * Token format: <base64url(payload)>.<base64url(hmac)>
 * Payload: { iat, exp, jti }
 *
 * Verification recomputes the HMAC and compares with timingSafeEqual, then
 * checks expiry and the in-memory/on-disk revocation list (jti set).
 */

const crypto = require('crypto');
const fs = require('fs');

function b64url(input) {
  return Buffer.from(input).toString('base64url');
}

function hmac(data, secret) {
  return crypto.createHmac('sha256', secret).update(data).digest();
}

function sign(payload, secret) {
  const body = b64url(JSON.stringify(payload));
  const signature = b64url(hmac(body, secret));
  return `${body}.${signature}`;
}

function issue(secret, ttlDays, extra) {
  const now = Math.floor(Date.now() / 1000);
  const payload = Object.assign(
    {
      iat: now,
      exp: now + Math.round((Number(ttlDays) || 30) * 86400),
      jti: crypto.randomBytes(12).toString('hex'),
    },
    extra || {}
  );
  return { token: sign(payload, secret), payload };
}

function verify(token, secret) {
  if (typeof token !== 'string' || typeof secret !== 'string' || !secret) return null;
  const dot = token.lastIndexOf('.');
  if (dot <= 0) return null;
  const body = token.slice(0, dot);
  const provided = token.slice(dot + 1);
  const expected = b64url(hmac(body, secret));
  const a = Buffer.from(provided);
  const b = Buffer.from(expected);
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return null;

  let payload;
  try {
    payload = JSON.parse(Buffer.from(body, 'base64url').toString('utf8'));
  } catch (_) {
    return null;
  }
  if (!payload || typeof payload !== 'object') return null;
  if (typeof payload.exp !== 'number' || !Number.isFinite(payload.exp)) return null;
  if (Math.floor(Date.now() / 1000) >= payload.exp) return null;
  if (payload.jti && revocation.has(payload.jti)) return null;
  return payload;
}

// ── Revocation list ────────────────────────────────────────────────────────
const revocation = new Set();
let revocationFile = null;

function initRevocation(file) {
  revocationFile = file;
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (Array.isArray(parsed)) parsed.forEach((jti) => revocation.add(jti));
  } catch (_) {
    /* no list yet */
  }
}

function persistRevocation() {
  if (!revocationFile) return;
  try {
    fs.writeFileSync(revocationFile, JSON.stringify([...revocation]));
  } catch (_) {
    /* best effort */
  }
}

function revoke(jti) {
  if (!jti) return;
  revocation.add(jti);
  persistRevocation();
}

module.exports = { b64url, sign, verify, issue, initRevocation, revoke };
