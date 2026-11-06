const assert = require('node:assert/strict');
const test = require('node:test');

const { producer } = require('../build/producer');
const { publishGroupSnapshotForGroup } = require('../build/event_handlers/groups');

test('publishes a joined group snapshot with its member roster', async () => {
  const events = [];
  const original = producer.publish;
  producer.publish = async (routingKey, payload) => events.push({ routingKey, payload });
  try {
    await publishGroupSnapshotForGroup({
      groupMetadata: async (id) => ({
        id, subject: 'Canary', desc: 'canary description',
        participants: [{ id: 'one@s.whatsapp.net', admin: 'admin' }],
      }),
    }, 'session_1', 'canary@g.us');
  } finally {
    producer.publish = original;
  }
  assert.deepEqual(events, [{
    routingKey: 'groups.update',
    payload: {
      chat_jid: 'canary@g.us', name: 'Canary', subject: 'Canary',
      description: 'canary description', desc: 'canary description',
      participant_count: 1, participants: [{ id: 'one@s.whatsapp.net', admin: 'admin' }],
      action: 'snapshot', session_name: 'session_1',
    },
  }]);
});
