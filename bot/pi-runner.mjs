// Pi owns tools, inference, retries and compaction; this only connects the Discord host.
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { createInterface } from 'node:readline';
import { codingTools } from './pi-tools.mjs';

const require = createRequire(`${process.env.EVO_PI_RUNTIME}/package.json`);
const { parse } = require('yaml');
const { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager,
  SettingsManager, createToolSearchExtension } =
  await import(`${process.env.EVO_PI_RUNTIME}/node_modules/@earendil-works/pi-coding-agent/dist/index.js`);
const { streamSimple } = await import(`${process.env.EVO_PI_RUNTIME}/node_modules/@earendil-works/pi-ai/dist/compat.js`);
const home = '/home/agent';
const modelId = process.argv[2];
const config = parse(readFileSync(`${home}/.pi/agent/config.yml`, 'utf8'));
const provider = parse(readFileSync(`${home}/.pi/agent/models.yml`, 'utf8')).providers.evo;
const pending = new Map();
let session, manager, filter, busy = false, compacting = false, stopping = false;

function emit(frame) {
  const bytes = Buffer.from(JSON.stringify(frame));
  if (bytes.length <= 512 * 1024) return process.stdout.write(bytes + '\n');
  const chunkId = randomUUID(), size = 384 * 1024;
  const count = Math.ceil(bytes.length / size);
  for (let index = 0; index < count; index++) process.stdout.write(JSON.stringify({
    type: 'rpc_chunk', chunkId, index, count, byteLength: bytes.length,
    data: bytes.subarray(index * size, (index + 1) * size).toString('base64'),
  }) + '\n');
}
function respond(command, data = {}) {
  emit({ type: 'response', id: command.id, command: command.type, success: true, data });
}

function hostCall(definition, args, signal) {
  const id = randomUUID();
  return new Promise((resolve, reject) => {
    const cancel = () => {
      pending.delete(id);
      emit({ type: 'host_tool_cancel', targetId: id });
      reject(new Error('Interrupted by user'));
    };
    if (signal?.aborted) return reject(new Error('Interrupted by user'));
    signal?.addEventListener('abort', cancel, { once: true });
    pending.set(id, {
      resolve: value => { signal?.removeEventListener('abort', cancel); resolve(value); },
      reject: error => { signal?.removeEventListener('abort', cancel); reject(error); },
    });
    emit({ type: 'host_tool_call', id, toolName: definition.name, arguments: args });
  });
}

// Preserve EVO's existing policy: discard reasoning only from completed earlier turns.
function completedTurnContext(context) {
  let start = -1;
  const remove = new Set();
  for (let end = 0; end < context.messages.length; end++) {
    if (context.messages[end].role !== 'user') continue;
    if (start >= 0 && context.messages.slice(start + 1, end)
      .findLast(m => m.role === 'assistant')?.stopReason === 'stop') {
      for (let i = start + 1; i < end; i++) if (context.messages[i].role === 'assistant') remove.add(i);
    }
    start = end;
  }
  return { ...context, messages: context.messages.map((m, i) => remove.has(i)
    ? { ...m, content: m.content.filter(b => b.type !== 'thinking') } : m) };
}

// Pi 1.4 renamed compaction events; preserve the host protocol and summary options.
function sessionEvent(event) {
  if (event.type === 'compaction_start') event = { ...event, type: 'auto_compaction_start' };
  if (event.type === 'compaction_end') event = { ...event, type: 'auto_compaction_end',
    result: event.result ? { ...event.result, tokensAfter: event.result.estimatedTokensAfter } : event.result };
  if (event.type === 'auto_compaction_start') compacting = true;
  if (event.type === 'auto_compaction_end') compacting = false;
  if (event.type !== 'agent_settled' && (!filter || filter.has(event.type))) emit(event);
}

async function initialize(definitions, transcript) {
  if (session) session.dispose();
  const settings = SettingsManager.inMemory({
    retry: config.retry, cacheWarming: 'off', defaultProjectTrust: 'never',
    compaction: { enabled: true, reserveTokens: config.compaction.reserveTokens,
      keepRecentTokens: config.compaction.keepRecentTokens },
    defaultTools: ['read', 'bash', 'edit', 'write', 'tool_search',
      ...definitions.filter(d => d.loadMode === 'essential').map(d => d.name)],
  });
  const runtime = await ModelRuntime.create({ authPath: `${home}/pi-auth.json`,
    modelsPath: null, refreshOnCreate: false });
  runtime.registerProvider('evo', {
    ...provider, api: 'evo-pi-completions',
    models: provider.models.map(m => ({ ...m, compat: { ...provider.compat, ...m.compat } })),
    streamSimple: (model, context, options = {}) => streamSimple(
      { ...model, api: 'openai-completions' }, compacting ? context : completedTurnContext(context), {
        ...options, maxHttpAttempts: 1,
        onPayload: async (payload, ...args) => {
          const body = await options.onPayload?.(payload, ...args) ?? payload;
          return { ...body, cache_prompt: true,
            ...(compacting && modelId === 'sc117-flash-next-iq3_xxs' ? {
              max_tokens: 0, final_output_max_tokens: 8192,
              reasoning_budget_tokens: 0, reasoning_loop_recovery: 'stop',
              chat_template_kwargs: { ...body.chat_template_kwargs, enable_thinking: true },
            } : {}),
          };
        },
      }),
  });
  manager = transcript ? SessionManager.open(transcript, `${home}/sessions`, '/workspace')
    : SessionManager.create('/workspace', `${home}/sessions`);
  const loader = new DefaultResourceLoader({
    cwd: '/workspace', agentDir: `${home}/pi`, settingsManager: settings,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true,
    noContextFiles: true, systemPrompt: readFileSync(`${home}/instructions.txt`, 'utf8'),
    extensionFactories: [createToolSearchExtension()],
  });
  await loader.reload();
  ({ session } = await createAgentSession({
    cwd: '/workspace', agentDir: `${home}/pi`, modelRuntime: runtime,
    model: runtime.getModel('evo', modelId), thinkingLevel: 'high',
    settingsManager: settings, resourceLoader: loader, sessionManager: manager,
    customTools: [...codingTools(hostCall), ...definitions.map(d => ({
      name: d.name, label: d.label ?? d.name, description: d.description,
      parameters: d.parameters, exposure: d.loadMode === 'essential' ? 'direct' : 'deferred',
      execute: async (_id, args, signal) => {
        const result = await hostCall(d, args, signal);
        return { ...result, details: undefined };
      },
    }))],
  }));
  // Several Discord addenda at the same tool boundary belong to the same correction.
  session.setSteeringMode('all');
  session.subscribe(sessionEvent);
  await session.bindExtensions({});
}

function state() {
  const queuedMessageCount = session.pendingMessageCount;
  return { model: session.model, isSettled: !busy && !compacting && !pending.size && !queuedMessageCount,
    isStreaming: session.isStreaming, isCompacting: compacting || session.isCompacting,
    hasPendingAsyncWork: pending.size > 0, queuedMessageCount,
    contextUsage: session.getContextUsage(), dumpTools: session.getAllTools(),
    sessionFile: manager.getSessionFile(), engine: 'pi', version: '1.4.0' };
}

let definitions = [];
async function command(c) {
  switch (c.type) {
    case 'negotiate_protocol': return respond(c, { protocolVersion: 2 });
    case 'set_event_filter': filter = c.events ? new Set(c.events) : null; return respond(c);
    case 'set_host_tools':
      if (session || busy) throw new Error('Tools are fixed for a session');
      definitions = c.tools; await initialize(definitions); return respond(c);
    case 'set_auto_retry': session.setAutoRetryEnabled(c.enabled); return respond(c);
    case 'set_auto_compaction': session.setAutoCompactionEnabled(c.enabled); return respond(c);
    case 'set_cache_warming': return respond(c); // Disabled explicitly in SettingsManager.
    case 'get_state': return respond(c, state());
    case 'get_messages': return respond(c, { messages: session.messages });
    case 'host_tool_result': {
      const call = pending.get(c.id);
      if (!call) return;
      pending.delete(c.id);
      if (c.isError) call.reject(new Error(c.result?.content?.find(b => b.type === 'text')?.text ?? 'Tool failed'));
      else call.resolve(c.result);
      return;
    }
    case 'prompt': {
      if (busy || compacting || stopping) throw new Error('Session is busy');
      busy = true;
      respond(c, { agentInvoked: true });
      let error;
      try {
        await session.prompt(c.message, { images: c.images });
        const last = [...session.messages].reverse().find(m => m.role === 'assistant');
        if (last?.stopReason === 'error') error = last.errorMessage ?? 'Provider failed';
        else if (last?.stopReason === 'aborted') error = 'Interrupted by user';
      } catch (e) { error = e.message; }
      finally {
        busy = false;
        emit({ type: 'prompt_result', id: c.id, agentInvoked: true,
          status: error ? 'error' : 'completed', error, sessionSettled: true });
        emit({ type: 'session_settled' });
      }
      return;
    }
    case 'steer':
    case 'follow_up': {
      // Discord additions must not become an idle queue entry after a turn ends.
      if (c.onlyIfActive && (!busy || !session.isStreaming))
        return respond(c, { disposition: 'not_running' });
      if (stopping || compacting) throw new Error('Session is stopping or compacting; retry after it settles');
      if (typeof c.message !== 'string' || !c.message.trim()) throw new Error('message must be a nonempty string');
      if (c.onlyIfActive && session.pendingMessageCount >= 16) throw new Error('Additional input queue is full');
      const disposition = c.type === 'steer'
        ? await session.steer(c.message, c.images, { source: 'rpc' })
        : await session.followUp(c.message, c.images, { source: 'rpc' });
      return respond(c, { disposition });
    }
    case 'clear_queue': return respond(c, session.clearQueue());
    case 'abort':
      stopping = true;
      try {
        // EVO /stop cancels queued inputs too; none may leak into the next owner task.
        session.clearQueue();
        await session.abort();
        return respond(c);
      } finally { stopping = false; }
    case 'compact':
      if (busy || compacting) throw new Error('Session is busy');
      compacting = true;
      try { return respond(c, await session.compact(c.customInstructions)); }
      finally { compacting = false; }
    case 'switch_session': {
      if (busy || compacting || pending.size) throw new Error('Session is busy');
      await initialize(definitions, c.sessionPath);
      return respond(c, { cancelled: false });
    }
    default: throw new Error(`Unsupported host command: ${c.type}`);
  }
}

const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
let incoming;
input.on('line', line => {
  let c;
  try {
    c = JSON.parse(line);
    if (c.type === 'rpc_chunk') {
      if (!incoming) {
        if (c.index !== 0 || !Number.isInteger(c.count) || c.count < 1 || c.count > 128
          || !Number.isInteger(c.byteLength) || c.byteLength < 1 || c.byteLength > 32 * 1024 * 1024)
          throw new Error('Invalid host chunks');
        incoming = { id: c.chunkId, count: c.count, bytes: c.byteLength, parts: [], size: 0 };
      }
      const x = incoming, part = Buffer.from(c.data, 'base64');
      if (x.id !== c.chunkId || x.parts.length !== c.index || x.count !== c.count
        || x.bytes !== c.byteLength || part.length > 384 * 1024 || x.size + part.length > x.bytes)
        throw new Error('Invalid host chunk sequence');
      x.parts.push(part); x.size += part.length;
      if (x.parts.length !== x.count) return;
      incoming = undefined;
      if (x.size !== x.bytes) throw new Error('Incomplete host chunks');
      c = JSON.parse(Buffer.concat(x.parts).toString('utf8'));
    } else if (incoming) throw new Error('Interleaved host chunks');
  }
  catch { return emit({ type: 'response', success: false, error: 'Invalid JSON frame' }); }
  command(c).catch(error => emit({ type: 'response', id: c.id, command: c.type,
    success: false, error: error.message }));
});
input.on('close', () => { session?.dispose(); process.exit(0); });
emit({ type: 'ready', engine: 'pi', version: '1.4.0' });
