// Real Pi SDK sessions with in-memory fake responses; no inference or live state.
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { guardSummaryStream } from './summary-guard.mjs';

const runtimePath = process.env.EVO_PI_RUNTIME;
assert(runtimePath, 'Set EVO_PI_RUNTIME to an installed Pi runtime');
const packagePath = join(runtimePath, 'node_modules/@earendil-works');
const sdk = await import(pathToFileURL(join(packagePath, 'pi-coding-agent/dist/index.js')));
const { createAssistantMessageEventStream, lazyStream } = await import(
  pathToFileURL(join(packagePath, 'pi-ai/dist/compat.js')));
const temporary = await mkdtemp(join(tmpdir(), 'evo-summary-guard-'));
const goal = 'OWNER-GOAL: preserve the invariant audit; do not run paid work.';
const correction = 'OWNER-CORRECTION: explain only, do not execute.';
const usage = { input: 1000, output: 30, cacheRead: 0, cacheWrite: 0, totalTokens: 1030,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
const cases = {
  empty: { content: [], stopReason: 'stop' },
  whitespace: { content: [{ type: 'text', text: ' \n\t ' }], stopReason: 'stop' },
  thinking: { content: [{ type: 'thinking', thinking: 'Not a final summary.' }], stopReason: 'stop' },
  aborted: { content: [{ type: 'text', text: 'Incomplete interrupted summary.' }], stopReason: 'aborted' },
  length: { content: [{ type: 'text', text: 'Incomplete capped summary.' }], stopReason: 'length' },
  error: { content: [], stopReason: 'error', errorMessage: 'Synthetic provider failure' },
  valid: { content: [{ type: 'text', text: `${goal}\n${correction}\nNEXT: inspect ownership.` }], stopReason: 'stop' },
};
const results = [];

async function check(mode, name, guarded) {
  const directory = join(temporary, `${mode}-${name}-${guarded}`);
  let compacting = false, summaryCalls = 0;
  const modelRuntime = await sdk.ModelRuntime.create({
    authPath: join(directory, 'auth.json'), modelsPath: null, refreshOnCreate: false,
  });
  modelRuntime.registerProvider('fixture', {
    api: 'fixture-summary', apiKey: 'synthetic-only', baseUrl: 'http://127.0.0.1:1/unused',
    models: [{ id: 'fixture', name: 'Fixture', reasoning: true, input: ['text'],
      contextWindow: 131072, maxTokens: 8192,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
    streamSimple(model) {
      const response = { role: 'assistant', api: model.api, provider: model.provider,
        model: model.id, usage: structuredClone(usage), timestamp: Date.now(),
        ...(compacting ? cases[name] : { content: [{ type: 'text', text: 'Fixture response.' }], stopReason: 'stop' }) };
      if (compacting) summaryCalls++;
      const stream = createAssistantMessageEventStream();
      queueMicrotask(() => {
        stream.push(response.stopReason === 'error' || response.stopReason === 'aborted'
          ? { type: 'error', reason: response.stopReason, error: response }
          : { type: 'done', reason: response.stopReason, message: response });
        stream.end(response);
      });
      return compacting && guarded ? guardSummaryStream(stream, model, lazyStream) : stream;
    },
  });
  const settingsManager = sdk.SettingsManager.inMemory({
    retry: { enabled: false }, cacheWarming: 'off', defaultProjectTrust: 'never', defaultTools: [],
    compaction: { enabled: false, reserveTokens: 1024, keepRecentTokens: 100 },
  });
  const sessionManager = sdk.SessionManager.create(directory, join(directory, 'sessions'));
  const resourceLoader = new sdk.DefaultResourceLoader({
    cwd: directory, agentDir: join(directory, 'agent'), settingsManager,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true,
    noContextFiles: true, systemPrompt: 'Synthetic controller. Preserve owner corrections.',
  });
  await resourceLoader.reload();
  const { session } = await sdk.createAgentSession({
    cwd: directory, agentDir: join(directory, 'agent'), modelRuntime,
    model: modelRuntime.getModel('fixture', 'fixture'), thinkingLevel: 'off',
    settingsManager, resourceLoader, sessionManager,
  });
  const events = [];
  session.subscribe(event => {
    events.push(event);
    if (event.type === 'compaction_start') compacting = true;
    if (event.type === 'compaction_end') compacting = false;
  });
  try {
    await session.prompt(goal);
    await session.prompt('Earlier fixture detail. '.repeat(300));
    await session.prompt(correction + '\nRecent fixture detail. '.repeat(300));
    const beforeEntries = structuredClone(sessionManager.getEntries());
    const beforeMessages = structuredClone(session.messages);
    assert(JSON.stringify(beforeMessages).includes(goal));
    let failure;
    if (mode === 'manual') {
      try { await session.compact(); } catch (error) { failure = error.message; }
    } else {
      settingsManager.setCompactionEnabled(true);
      // Exercise the same native path used by automatic threshold/overflow handling.
      await session._runAutoCompaction('threshold', false);
      failure = events.findLast(event => event.type === 'compaction_end')?.errorMessage;
    }
    assert.equal(summaryCalls, 1, 'Only one synthetic summary attempt is allowed');
    const entries = sessionManager.getEntries();
    const committed = entries.some(entry => entry.type === 'compaction');
    if (guarded && name !== 'valid') {
      assert(failure, `${mode}/${name} must report summary failure`);
      assert.equal(committed, false, 'Invalid summary must not be committed');
      assert.deepEqual(entries, beforeEntries, 'Persisted active history must be unchanged');
      assert.deepEqual(session.messages, beforeMessages, 'Active model context must be unchanged');
    } else {
      assert.equal(failure, undefined);
      assert.equal(committed, true);
      assert.equal(JSON.stringify(session.messages).includes(goal), name === 'valid');
      if (name === 'valid') assert(JSON.stringify(session.messages).includes(correction));
    }
    results.push({ mode, case: name, guarded, committed, history_preserved: !committed,
      active_goal_retained: JSON.stringify(session.messages).includes(goal) });
  } finally { session.dispose(); }
}

try {
  for (const mode of ['manual', 'automatic']) {
    await check(mode, 'empty', false); // Reproduce native empty-summary context loss.
    for (const name of Object.keys(cases)) await check(mode, name, true);
  }
  console.log(JSON.stringify({ passed: results.length, real_model_calls: 0, live_state_changes: 0, results }, null, 2));
} finally { await rm(temporary, { recursive: true, force: true }); }
