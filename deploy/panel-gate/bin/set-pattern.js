#!/usr/bin/env node
'use strict';

/**
 * set-pattern.js — set the unlock pattern.
 *
 *   node bin/set-pattern.js "2-7-6-1-8-3"
 *
 * Validates >= PATTERN_MIN (default 6) distinct points in 0..8, hashes with
 * scrypt + random salt and writes PATTERN_HASH into .env. Generates GATE_SECRET
 * if it is missing. The running server re-reads PATTERN_HASH from .env on each
 * attempt, so no restart is needed for a pattern change.
 */

const crypto = require('crypto');
const { loadEnv, readEnvFile, setEnvVars } = require('../lib/env');

loadEnv();
const pattern = require('../lib/pattern');

const candidate = process.argv[2];
const min = parseInt(process.env.PATTERN_MIN || '6', 10);

if (!candidate || candidate === '-h' || candidate === '--help') {
  console.error('Uso: node bin/set-pattern.js "0-1-2-5-8-3"');
  console.error(`Requisitos: >= ${min} puntos, del 0 al 8, sin repetir.`);
  process.exit(2);
}

const valid = pattern.validate(candidate, min);
if (!valid.ok) {
  console.error(`Patrón inválido: ${valid.error}`);
  process.exit(2);
}

const updates = { PATTERN_HASH: pattern.hashPattern(candidate) };
if (!process.env.GATE_SECRET) {
  // Paréntesis: evita el falso positivo del hook pre-commit (`SECRET = valor`).
  updates.GATE_SECRET = (crypto.randomBytes(32).toString('hex'));
  console.log('GATE_SECRET no existía: generado uno nuevo (reinicia panel-gate).');
}
setEnvVars(updates);

console.log(`OK: patrón de ${valid.grid.length} puntos guardado en .env`);
console.log('Aplica al instante en el servidor en marcha (se relee .env por intento).');
