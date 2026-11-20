'use strict';

// The key of a row under "Models for different purposes" follows its model (assets/js/settings.js). Run: node --test
const test = require('node:test');
const assert = require('node:assert/strict');
const { purposeKeyChoice } = require('../../assets/js/settings.js');

const PROVIDER_KEYS = { gemini: 'GEMINI_API_KEY', anthropic: 'ANTHROPIC_API_KEY', openai: 'OPENAI_API_KEY' };
const DEFAULT = { value: '', service: '', custom: false, set: false };
const GEMINI = { value: 'GEMINI_API_KEY', service: 'gemini', custom: false, set: true };
const ANTHROPIC = { value: 'ANTHROPIC_API_KEY', service: 'anthropic', custom: false, set: true };
const TEAM = { value: 'ANTHROPIC_TEAM_KEY', service: '', custom: false, set: true };   // another name the server allows
const ACME = { value: 'ACME_API_KEY', service: '', custom: true, set: true };          // added under a name of its own

function choose(model, current, auto, options, onLoad = false) {
  return purposeKeyChoice(model, current, auto, [DEFAULT, ...options], PROVIDER_KEYS, onLoad);
}

test('a model of a known service takes its key when that is the only one that fits', () => {
  assert.deepEqual(choose('anthropic/claude-haiku-4-5', '', false, [GEMINI, ANTHROPIC]), { value: 'ANTHROPIC_API_KEY', auto: true });
  assert.deepEqual(choose('gemini/gemini-2.5-flash', 'ANTHROPIC_API_KEY', true, [GEMINI, ANTHROPIC]), { value: 'GEMINI_API_KEY', auto: true });
});

test('without its service key set, another service key is let go', () => {
  assert.deepEqual(choose('anthropic/claude-haiku-4-5', 'GEMINI_API_KEY', false, [GEMINI]), { value: '', auto: false });
  assert.equal(choose('anthropic/claude-haiku-4-5', '', false, [GEMINI]), null);
});

test('with several keys that fit, the sysadmin chooses, and a fitting choice stays', () => {
  assert.equal(choose('anthropic/claude-sonnet-4-5', '', false, [ANTHROPIC, TEAM]), null);
  assert.equal(choose('anthropic/claude-sonnet-4-5', 'ANTHROPIC_TEAM_KEY', false, [ANTHROPIC, TEAM]), null);
});

test('a key added under a name of its own is never chosen by the script', () => {
  assert.equal(choose('ollama/acme', '', false, [ACME]), null);
  assert.equal(choose('ollama/acme', 'ACME_API_KEY', false, [ACME]), null);                         // chosen by hand: stays
  assert.deepEqual(choose('anthropic/claude-haiku-4-5', 'ACME_API_KEY', false, [ACME, ANTHROPIC]),  // never to a known service
                   { value: 'ANTHROPIC_API_KEY', auto: true });
  assert.deepEqual(choose('ollama/acme', 'ANTHROPIC_API_KEY', false, [ACME, ANTHROPIC]), { value: '', auto: false });
});

test('only a key the script chose is let go for an empty model or one without a prefix', () => {
  assert.deepEqual(choose('', 'ANTHROPIC_API_KEY', true, [ANTHROPIC]), { value: '', auto: false });
  assert.deepEqual(choose('gpt-4o', 'ANTHROPIC_API_KEY', true, [ANTHROPIC]), { value: '', auto: false });
  assert.equal(choose('', 'ACME_API_KEY', false, [ACME]), null);
  assert.equal(choose('gpt-4o', 'TEAM', false, [TEAM]), null);
});

test('on load a saved row shows its key, and an explicit choice is not touched', () => {
  assert.deepEqual(choose('anthropic/claude-haiku-4-5', '', false, [ANTHROPIC], true), { value: 'ANTHROPIC_API_KEY', auto: true });
  assert.equal(choose('anthropic/claude-haiku-4-5', 'ANTHROPIC_TEAM_KEY', false, [ANTHROPIC, TEAM], true), null);
  assert.equal(choose('', '', false, [ANTHROPIC], true), null);
});
