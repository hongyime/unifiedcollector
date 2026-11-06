const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const test = require('node:test');

const { producer } = require('../build/producer');
const { registerMessagesHandler } = require('../build/event_handlers/messages');

async function publishFrom(message) {
  const events = [];
  const original = producer.publish;
  producer.publish = async (routingKey, payload) => events.push({ routingKey, payload });
  try {
    const sock = { ev: new EventEmitter(), user: { id: 'owner@s.whatsapp.net' } };
    registerMessagesHandler(sock);
    sock.ev.emit('messages.upsert', { messages: [message] });
    await new Promise((resolve) => setImmediate(resolve));
    return events;
  } finally {
    producer.publish = original;
  }
}

test('publishes an inbound reaction with its target and actor', async () => {
  const events = await publishFrom({
    key: { id: 'reaction-1', remoteJid: 'chat@g.us', participant: 'actor@s.whatsapp.net' },
    messageTimestamp: 100,
    message: { reactionMessage: { key: { id: 'target-1', remoteJid: 'chat@g.us' }, text: '👍' } },
  });
  assert.deepEqual(events, [{
    routingKey: 'messages.reaction',
    payload: {
      reaction: true, message_id: 'reaction-1', reaction_message_id: 'reaction-1',
      target_message_id: 'target-1', chat_jid: 'chat@g.us', actor_jid: 'actor@s.whatsapp.net',
      emoji: '👍', timestamp: 100, session_name: 'default', account_wid: 'owner@s.whatsapp.net', routing_key: 'messages.reaction',
    },
  }]);
});

test('publishes a poll definition without attempting to process encrypted votes', async () => {
  const events = await publishFrom({
    key: { id: 'poll-1', remoteJid: 'chat@g.us', participant: 'creator@s.whatsapp.net' },
    messageTimestamp: 200,
    message: { pollCreationMessage: {
      name: 'Lunch?', options: [{ optionName: 'Yes' }, { optionName: 'No' }], selectableOptionsCount: 1,
    } },
  });
  assert.deepEqual(events, [{
    routingKey: 'messages.poll',
    payload: {
      poll: true, message_id: 'poll-1', chat_jid: 'chat@g.us', sender_jid: 'creator@s.whatsapp.net',
      chat_name: 'chat', question: 'Lunch?', options: ['Yes', 'No'], selectable_options_count: 1,
      poll_type: 'poll', timestamp: 200, session_name: 'default', account_wid: 'owner@s.whatsapp.net', routing_key: 'messages.poll',
    },
  }]);
});
