'use strict';

/**
 * lib/env.js — minimal .env loader/updater.
 *
 * Node 20.5.1 does not support --env-file (added in 20.6), so we parse the file
 * ourselves. The same helpers are used by server.js and the bin/ CLIs so there
 * is a single source of truth for the file format.
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const ENV_PATH = path.join(ROOT, '.env');

function stripQuotes(value) {
  if (
    (value.startsWith('"') && value.endsWith('"')) ||
    (value.startsWith("'") && value.endsWith("'"))
  ) {
    return value.slice(1, -1);
  }
  return value;
}

/** Parse `.env` into a plain object (does not touch process.env). */
function readEnvFile() {
  const out = {};
  if (!fs.existsSync(ENV_PATH)) return out;
  const text = fs.readFileSync(ENV_PATH, 'utf8');
  for (const rawLine of text.split('\n')) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq < 0) continue;
    const key = line.slice(0, eq).trim();
    const value = stripQuotes(line.slice(eq + 1).trim());
    if (key) out[key] = value;
  }
  return out;
}

/** Load `.env` values into process.env without overriding real environment. */
function loadEnv() {
  const values = readEnvFile();
  for (const [key, value] of Object.entries(values)) {
    if (process.env[key] === undefined) process.env[key] = value;
  }
  return values;
}

/**
 * Update/append keys in `.env`, preserving comments and ordering of the rest.
 * The file is written with mode 0600 (contains the gate secret and hash).
 */
function setEnvVars(updates) {
  let lines = [];
  if (fs.existsSync(ENV_PATH)) {
    lines = fs.readFileSync(ENV_PATH, 'utf8').split('\n');
  }
  const seen = new Set();
  const out = lines.map((line) => {
    const match = line.match(/^\s*([A-Za-z0-9_]+)\s*=/);
    if (match && Object.prototype.hasOwnProperty.call(updates, match[1])) {
      seen.add(match[1]);
      return `${match[1]}=${updates[match[1]]}`;
    }
    return line;
  });
  for (const [key, value] of Object.entries(updates)) {
    if (!seen.has(key)) out.push(`${key}=${value}`);
  }
  let text = out.join('\n').replace(/\n+$/, '\n');
  if (!text.endsWith('\n')) text += '\n';
  fs.writeFileSync(ENV_PATH, text, { mode: 0o600 });
  try {
    fs.chmodSync(ENV_PATH, 0o600);
  } catch (_) {
    /* best effort */
  }
}

module.exports = { ROOT, ENV_PATH, readEnvFile, loadEnv, setEnvVars };
