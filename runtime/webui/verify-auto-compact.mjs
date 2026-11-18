import assert from 'node:assert/strict';
import { test } from 'node:test';
import { prepareWebContext, fetchWithWebCompaction } from './auto-compact.js';

function fixture(summary = 'Goal: compare the previously inspected images. Continue the current task.') {
  let checkpoint, summaryCalls = 0, chatCalls = 0;
  const ctx = {
    conversationId: 'fixture', contextSize: 32768, url: '/v1/chat/completions',
    load: async () => checkpoint,
    save: async cp => { checkpoint = { webContextCheckpoint: cp }; },
    fetch: async (url, options) => {
      const body = JSON.parse(options.body);
      if (url.includes('count_tokens')) {
        return Response.json({ input_tokens: Math.ceil(JSON.stringify(body).length / 4) });
      }
      if (body.messages[0]?.content?.startsWith('Summarize the supplied conversation')) {
        summaryCalls++;
        return new Response('data: ' + JSON.stringify({ choices: [{
          delta: { content: summary }, finish_reason: 'stop'
        }] }) + '\n\ndata: [DONE]\n\n');
      }
      chatCalls++;
      return chatCalls === 1
        ? Response.json({ error: { message: 'request exceeds the context window' } }, { status: 400 })
        : Response.json({ choices: [{ message: { content: 'done' } }] });
    }
  };
  return { ctx, saved: () => checkpoint, summaries: () => summaryCalls, chats: () => chatCalls };
}

test('image-heavy history compacts without leaving an oversized recent tail', async () => {
  const { ctx, saved } = fixture();
  const messages = [{ role: 'user', content: 'Earlier investigation. '.repeat(4000) }];
  for (let i = 0; i < 9; i++) messages.push(
    { role: 'user', content: [
      { type: 'text', text: 'Inspect screenshot ' + i },
      { type: 'image_url', image_url: { url: 'data:image/png;base64,fixture' + i } }
    ] },
    { role: 'assistant', content: 'Verified screenshot ' + i + ': an observed result.' }
  );
  messages.push({ role: 'user', content: 'CURRENT_GOAL: continue the comparison.' });
  const body = { model: 'sc117-flash-next-iq3_xxs', messages };
  const original = structuredClone(body);
  const prepared = await prepareWebContext(body, ctx);
  assert(saved()?.webContextCheckpoint);
  assert.deepEqual(body, original);
  assert.equal(prepared.messages.at(-1).content, messages.at(-1).content);
  const images = prepared.messages.flatMap(m => Array.isArray(m.content) ? m.content : [])
    .filter(p => p.type === 'image_url').length;
  assert(images < 9, 'old screenshots must be accounted for when choosing the summary boundary');
});

test('context-error recovery accepts a fitting summary even when the counter previously undercounted', async () => {
  const f = fixture('The current goal and prior verified results. '.repeat(40));
  const body = { model: 'sc117-flash-next-iq3_xxs', messages: [
    { role: 'user', content: 'Earlier question.' },
    { role: 'assistant', content: 'Earlier answer.' },
    { role: 'user', content: 'CURRENT_GOAL: continue.' }
  ] };
  const response = await fetchWithWebCompaction('/v1/chat/completions', body,
    { method: 'POST', body: JSON.stringify(body) }, f.ctx);
  assert.equal(response.status, 200);
  assert.equal(f.chats(), 2);
  assert.equal(f.summaries(), 1);
  assert(f.saved()?.webContextCheckpoint);
});

test('oversized latest input is preserved and rejected without saving a bad checkpoint', async () => {
  const f = fixture();
  const body = { model: 'sc117-flash-next-iq3_xxs', messages: [
    { role: 'user', content: 'Earlier task.' },
    { role: 'assistant', content: 'Earlier result.' },
    { role: 'user', content: Array.from({ length: 9 }, (_, i) => ({
      type: 'image_url', image_url: { url: 'data:image/png;base64,latest' + i }
    })) }
  ] };
  const original = structuredClone(body);
  await assert.rejects(prepareWebContext(body, f.ctx), /압축 후에도 여유/);
  assert.deepEqual(body, original);
  assert.equal(f.saved(), undefined);
});

test('latest screenshots remain intact when older screenshots are summarized', async () => {
  const f = fixture();
  const last = { role: 'user', content: Array.from({ length: 3 }, (_, i) => ({
    type: 'image_url', image_url: { url: 'data:image/png;base64,current' + i }
  })) };
  const body = { model: 'sc117-flash-next-iq3_xxs', messages: [
    { role: 'user', content: 'Prior investigation. '.repeat(6000) },
    { role: 'assistant', content: 'Prior findings.' }, last
  ] };
  const prepared = await prepareWebContext(body, f.ctx);
  assert.deepEqual(prepared.messages.at(-1), last);
  assert(f.saved()?.webContextCheckpoint);
});
