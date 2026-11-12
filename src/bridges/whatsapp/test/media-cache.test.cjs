const test = require('node:test');
const assert = require('node:assert/strict');
const { rememberMedia, takeMedia } = require('../build/media_cache.js');

test('takeMedia returns a cached message once', () => {
  const message = { key: { id: `one-shot-${Date.now()}` }, message: { conversation: 'payload' } };
  rememberMedia(message);
  assert.equal(takeMedia(message.key.id), message);
  assert.equal(takeMedia(message.key.id), null);
});
