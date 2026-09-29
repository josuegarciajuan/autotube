#!/usr/bin/env node
'use strict';

/**
 * hash-secret.js — (re)generate the HMAC session secret (GATE_SECRET).
 *
 *   node bin/hash-secret.js
 *
 * Writes a fresh 64-hex-char random secret into .env. Restart panel-gate to
 * apply it; all previously issued sessions become invalid.
 */

const crypto = require('crypto');
const fs = require('fs');
const { ENV_PATH, readEnvFile, setEnvVars } = require('../lib/env');

if (!fs.existsSync(ENV_PATH)) {
  console.error('No existe .env. Copia .env.example y ejecútalo de nuevo.');
  process.exit(1);
}

// Ensure the file is parseable before touching it.
readEnvFile();
setEnvVars({ GATE_SECRET: crypto.randomBytes(32).toString('hex') });

console.log('OK: GATE_SECRET regenerado en .env (64 hex).');
console.log('Reinicia panel-gate para aplicarlo: systemctl restart panel-gate');
console.log('Las cookies de sesión emitidas antes quedarán invalidadas.');
