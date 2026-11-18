// Pi owns tools, inference, retries and compaction; this only connects the Discord host.
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { createInterface } from 'node:readline';
import { codingTools } from './pi-tools.mjs';
import { guardSummaryStream } from './summary-guard.mjs';

const require = createRequire(`${process.env.EVO_PI_RUNTIME}/package.json`);
const engineVersion = JSON.parse(readFileSync(`${process.env.EVO_PI_RUNTIME}/node_modules/@earendil-works/pi-coding-agent/package.json`, 'utf8')).version;
const { parse } = require('yaml');
const { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager,
  SettingsManager, createToolSearchExtension } =
  await import(`${process.env.EVO_PI_RUNTIME}/node_modules/@earendil-works/pi-coding-agent/dist/index.js`);
const { streamSimple, lazyStream } = await import(`${process.env.EVO_PI_RUNTIME}/node_modules/@earendil-works/pi-ai/dist/compat.js`);
const home = '/home/agent';
const modelId = process.argv[2];
const config = parse(readFileSync(`${home}/.pi/agent/config.yml`, 'utf8'));
const provider = parse(readFileSync(`${home}/.pi/agent/models.yml`, 'utf8')).providers.evo;
const pending = new Map();
let session, manager, filter, busy = false, compacting = false, stopping = false;
let inputTail = Promise.resolve(), inputCount = 0, inputEpoch = 0, aborting;
let inputRevision = 0;
let finalRecoveryUsed = false, finalRecoveryPending = false, answerOnlyRequest = false;
let taskId = '', taskReported = false, taskRepair = false, taskRepairCalls = 0;
let yieldRequest, promptDisposition = '';
const CHECKPOINT_POLICY = 'Checkpoint only the current unfinished task and explicitly reusable user preferences. '
  + 'Completed, cancelled and superseded requests are historical facts, never standing authorization. '
  + 'Revalidate previous-summary items against newer messages; omit obsolete operational steps. '
  + 'Preserve the distinction between permission for one task and a durable rule. Do not invent future obligations.';

function emptyFinal(message) {
  return message?.role === 'assistant' && message.stopReason === 'stop'
    && !message.content.some(b => b.type === 'toolCall' || b.type === 'tool_call'
      || b.type === 'text' && b.text?.trim());
}

function editInputs(action) {
  const result = inputTail.then(action);
  inputTail = result.catch(() => {});
  return result;
}

function emitSettled() {
  if (session && state().isSettled) emit({ type: 'session_settled' });
}

function queueInput(c) {
  if (typeof c.message !== 'string' || !c.message.trim()) throw new Error('message must be a nonempty string');
  if (Buffer.byteLength(JSON.stringify(c)) > 1024 * 1024) throw new Error('Additional input exceeds 1 MiB');
  if (c.images !== undefined && (!Array.isArray(c.images) || c.images.some(image =>
    image?.type !== 'image' || typeof image.data !== 'string' || typeof image.mimeType !== 'string')))
    throw new Error('images must contain image blocks');
  if (stopping || compacting) throw new Error('Session is stopping or compacting; retry after it settles');
  if (session.pendingMessageCount + inputCount >= 16) throw new Error('Additional input queue is full (16)');
  const epoch = inputEpoch;
  inputCount++;
  return editInputs(async () => {
    try {
      if (stopping || epoch !== inputEpoch) throw new Error('Additional input cancelled');
      // Recheck inside the queue: preflight and a concurrent abort may have yielded.
      if (c.onlyIfActive && (!busy || !session.isStreaming)) return { disposition: 'not_running' };
      const disposition = c.type === 'steer'
        ? await session.steer(c.message, c.images, { source: 'rpc' })
        : await session.followUp(c.message, c.images, { source: 'rpc' });
      if (epoch !== inputEpoch) throw new Error('Additional input cancelled');
      if (disposition === 'queued') {
        finalRecoveryPending = false;
      }
      if (disposition === 'queued' || disposition === 'handled') {
        inputRevision++;
        yieldRequest = undefined;
        promptDisposition = '';
        if (taskId) taskReported = false;
      }
      return { disposition };
    } finally {
      inputCount--;
      emitSettled();
    }
  });
}

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
    emit({ type: 'host_tool_call', id, toolName: definition.name,
      ...(definition.displayName ? { displayName: definition.displayName } : {}), arguments: args });
  });
}

// Preserve EVO's existing policy: discard reasoning only from completed earlier turns.
// Completed turns also drop their screenshots: each screenshot costs a fixed 1,077 tokens
// (measured) and is re-prefilled on every later request. Only the newest IMAGES_KEEP images
// survive anywhere in the context; the live turn keeps everything it needs for clicks.
const IMAGES_KEEP = 2;
function completedTurnContext(context) {
  const messages = context.messages;
  let start = -1;
  const reasoning = new Set();
  const completed = new Set();
  for (let end = 0; end < messages.length; end++) {
    if (messages[end].role !== 'user') continue;
    if (start >= 0 && messages.slice(start + 1, end)
      .findLast(m => m.role === 'assistant')?.stopReason === 'stop') {
      for (let i = start + 1; i < end; i++) {
        completed.add(i);
        if (messages[i].role === 'assistant') reasoning.add(i);
      }
    }
    start = end;
  }
  const images = [];
  messages.forEach((m, i) => { if (Array.isArray(m.content)) m.content.forEach((b, j) => { if (b.type === 'image') images.push(`${i}:${j}`); }); });
  const keep = new Set(images.slice(-IMAGES_KEEP));
  return { ...context, messages: messages.map((m, i) => {
    const dropReasoning = reasoning.has(i);
    const content = Array.isArray(m.content) ? m.content : [];
    const dropImages = completed.has(i) && content.some((b, j) => b.type === 'image' && !keep.has(`${i}:${j}`));
    if (!dropReasoning && !dropImages) return m;
    const left = content.filter((b, j) => (!dropReasoning || b.type !== 'thinking')
      && (!dropImages || b.type !== 'image' || keep.has(`${i}:${j}`)));
    if (left.length === 0 && dropImages) left.push({ type: 'text', text: '[older screenshot omitted]' });
    return { ...m, content: left };
  }) };
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
    defaultTools: ['read', 'exec', 'edit', 'write', 'tool_search',
      ...definitions.filter(d => d.loadMode === 'essential').map(d => d.name)],
  });
  const runtime = await ModelRuntime.create({ authPath: `${home}/pi-auth.json`,
    modelsPath: null, refreshOnCreate: false });
  runtime.registerProvider('evo', {
    ...provider, api: 'evo-pi-completions',
    models: provider.models.map(m => ({ ...m, compat: { ...provider.compat, ...m.compat } })),
    streamSimple: (model, context, options = {}) => {
      const finalOnly = !compacting && finalRecoveryPending;
      if (!compacting) {
        answerOnlyRequest = finalOnly;
        finalRecoveryPending = false;
      }
      const output = streamSimple(
      { ...model, api: 'openai-completions' }, compacting ? {
        ...context, systemPrompt: (context.systemPrompt ?? '') + '\n' + CHECKPOINT_POLICY,
      } : completedTurnContext(context), {
        ...options, maxHttpAttempts: 1,
        onPayload: async (payload, ...args) => {
          const body = await options.onPayload?.(payload, ...args) ?? payload;
          return { ...body, cache_prompt: true,
            ...(finalOnly ? { tools: [], tool_choice: 'none',
              chat_template_kwargs: { ...body.chat_template_kwargs, enable_thinking: false } } : {}),
            ...(compacting && modelId === 'sc117-flash-next-iq3_xxs' ? {
              max_tokens: 0, final_output_max_tokens: 8192,
              reasoning_budget_tokens: 0, reasoning_loop_recovery: 'recover',
              chat_template_kwargs: { ...body.chat_template_kwargs, enable_thinking: true },
            } : {}),
          };
        },
      });
      return compacting ? guardSummaryStream(output, model, lazyStream) : output;
    },
  });
  manager = transcript ? SessionManager.open(transcript, `${home}/sessions`, '/workspace')
    : SessionManager.create('/workspace', `${home}/sessions`);
  const loader = new DefaultResourceLoader({
    cwd: '/workspace', agentDir: `${home}/pi`, settingsManager: settings,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true,
    noContextFiles: true, systemPrompt: readFileSync(`${home}/instructions.txt`, 'utf8'),
    extensionFactories: [createToolSearchExtension(), pi => {
      // Pi must not settle while an accepted input is still being expanded.
      pi.on('agent_before_settle', async event => {
        while (inputCount) await inputTail;
        if (promptDisposition && !stopping && !session.pendingMessageCount) return;
        if (event.outcome === 'completed' && !stopping && !session.pendingMessageCount
            && taskId && !taskReported && !taskRepair) {
          taskRepair = true;
          return { continue: true, entries: [{ type: 'custom_message', customType: 'evo-task-state',
            display: false, content: `Task ${taskId} has no progress state. Report existing results only: `
              + 'discover schedule if needed, then schedule(action=note,text=brief checkpoint/evidence,'
              + 'state=continue|waiting|done|blocked). No other work or tools in this bookkeeping step. '
              + 'done requires verified completion; no reply grants no permission.' }] };
        }
        if (event.outcome !== 'completed' || stopping || session.pendingMessageCount
            || finalRecoveryUsed || !emptyFinal(session.messages.at(-1))) return;
        finalRecoveryUsed = finalRecoveryPending = true;
        emit({ type: 'final_answer_recovery', attempt: 1 });
        return { continue: true, entries: [{ type: 'custom_message', customType: 'evo-final-answer',
          display: false, content: 'The last response ended without a user-facing answer. '
            + 'Give the final answer from existing results, or state what remains unverified. '
            + 'Do not repeat actions or call tools. A newer owner instruction takes precedence.' }] };
      });
      // Enforce the recovery restriction even if a provider ignores tool_choice=none.
      pi.on('tool_call', event => {
        yieldRequest = undefined;
        promptDisposition = '';
        const pause = event.toolName === 'schedule' && (event.input?.state === 'waiting'
          || taskId && ['defer', 'enqueue'].includes(event.input?.action)
          || !taskId && event.input?.action === 'enqueue' && event.input.handoff === true);
        if (pause && session.messages.at(-1)?.content.filter(b => b.type === 'toolCall').length > 1)
          return { block: true, reason: 'Save waiting/enqueue in a separate tool call after other actions finish.' };
        if (taskId && !['schedule', 'tool_search'].includes(event.toolName)) taskReported = false;
        if (taskRepair && (++taskRepairCalls > 3 || !(event.toolName === 'tool_search'
            || event.toolName === 'schedule' && ['read', 'note'].includes(event.input?.action))))
          return { block: true, terminate: true, reason: 'Task bookkeeping permits only schedule read/note (maximum 3 calls).' };
        if (answerOnlyRequest)
          return { block: true, terminate: true, reason: 'Final-answer recovery cannot execute tools.' };
      });
      pi.on('turn_end', () => { answerOnlyRequest = false; });
    }],
  });
  await loader.reload();
  ({ session } = await createAgentSession({
    cwd: '/workspace', agentDir: `${home}/pi`, modelRuntime: runtime,
    model: runtime.getModel('evo', modelId), thinkingLevel: 'high',
    settingsManager: settings, resourceLoader: loader, sessionManager: manager,
    excludeTools: ['bash', 'powershell'],
    customTools: [...codingTools(hostCall), ...definitions.map(d => ({
      name: d.name, label: d.label ?? d.name, description: d.description,
      parameters: d.parameters, exposure: d.loadMode === 'essential' ? 'direct' : 'deferred',
      execute: async (_id, args, signal) => {
        const revision = inputRevision;
        const result = await hostCall(d, args, signal);
        if (d.name === 'schedule') {
          try {
            const report = JSON.parse(result.content?.find(b => b.type === 'text')?.text ?? '{}');
            if (revision === inputRevision && !inputCount && !session.pendingMessageCount
                && taskId && (report.saved || report.success) && (report.job_id || report.id) === taskId
                && ['continue', 'waiting', 'done', 'blocked'].includes(report.state)) {
              taskReported = true;
              if (report.state === 'waiting') yieldRequest = { disposition: 'waiting', taskId };
            } else if (revision === inputRevision && !inputCount && !session.pendingMessageCount
                && !taskId && args.action === 'enqueue' && args.handoff === true && report.handoff === true
                && report.success === true && report.continuation === true && report.mode === 'task'
                && typeof report.id === 'string') {
              yieldRequest = { disposition: 'queued', taskId: report.id };
            }
          } catch (_) { /* A malformed result cannot confirm task completion. */ }
        }
        if (d.name === 'user_input' && !signal?.aborted && !stopping) {
          try {
            const reply = JSON.parse(result.content?.find(b => b.type === 'text')?.text ?? '{}');
            if (reply.status === 'answered' && reply.answered === true && typeof reply.answer === 'string') {
              const images = result.content.filter(b => b.type === 'image');
              const { disposition } = await queueInput({ type: 'steer', onlyIfActive: true,
                message: reply.answer || '첨부 이미지를 확인해줘.', images });
              if (disposition === 'queued' || disposition === 'handled') {
                emit({ type: 'owner_reply_forwarded', disposition });
                return { content: [{ type: 'text', text: JSON.stringify({ status: 'answered',
                  answered: true, delivered_as: 'user_message' }) }] };
              }
              emit({ type: 'owner_reply_fallback' });
            }
          } catch (_) {
            // Preserve the original answer if forwarding cannot be confirmed; never replay input.
            emit({ type: 'owner_reply_fallback' });
          }
        }
        return { ...result, details: undefined };
      },
    }))],
  }));
  const finishTurn = session.agent.finishTurn;
  session.agent.finishTurn = async (turn, signal) => {
    const decision = await finishTurn?.(turn, signal);
    if (yieldRequest && !inputCount && !stopping && !session.pendingMessageCount && !pending.size
        && !['error', 'aborted'].includes(turn.message.stopReason)) {
      promptDisposition = yieldRequest.disposition;
      return { action: 'end' };
    }
    return decision;
  };
  // Several Discord addenda at the same tool boundary belong to the same correction.
  session.setSteeringMode('all');
  session.setFollowUpMode('one-at-a-time');
  session.subscribe(sessionEvent);
  await session.bindExtensions({});
}

function state() {
  const queuedMessageCount = session.pendingMessageCount;
  return { model: session.model, isSettled: !busy && !stopping && !inputCount && !compacting
      && !session.isStreaming && !session.isCompacting && !pending.size && !queuedMessageCount,
    isStreaming: session.isStreaming, isCompacting: compacting || session.isCompacting,
    hasPendingAsyncWork: pending.size > 0 || inputCount > 0 || stopping, queuedMessageCount,
    contextUsage: session.getContextUsage(), dumpTools: session.getAllTools(),
    sessionFile: manager.getSessionFile(), engine: 'pi', version: engineVersion };
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
      if (busy || compacting || stopping || inputCount) throw new Error('Session is busy');
      busy = true;
      finalRecoveryUsed = finalRecoveryPending = answerOnlyRequest = false;
      taskId = typeof c.task_id === 'string' ? c.task_id : '';
      taskReported = taskRepair = false; taskRepairCalls = 0;
      yieldRequest = undefined; promptDisposition = '';
      respond(c, { agentInvoked: true });
      let error;
      try {
        await session.prompt(c.message, { images: c.images });
        const last = [...session.messages].reverse().find(m => m.role === 'assistant');
        if (last?.stopReason === 'error') error = last.errorMessage ?? 'Provider failed';
        else if (last?.stopReason === 'aborted') error = 'Interrupted by user';
        else if (finalRecoveryUsed && !last?.content.some(b => b.type === 'text' && b.text?.trim()))
          error = '모델이 답변 복구 1회 후에도 최종 답변을 반환하지 않았습니다. 도구 작업은 재실행하지 않았습니다.';
      } catch (e) { error = e.message; }
      finally {
        const taskReportConfirmed = !taskId || taskReported;
        busy = false;
        taskId = ''; taskRepair = false;
        finalRecoveryPending = answerOnlyRequest = false;
        emit({ type: 'prompt_result', id: c.id, agentInvoked: true,
          status: error ? 'error' : 'completed', error, taskReported: taskReportConfirmed,
          ...(!error && promptDisposition ? { disposition: promptDisposition, taskId: yieldRequest.taskId } : {}),
          sessionSettled: state().isSettled });
        yieldRequest = undefined; promptDisposition = '';
        emitSettled();
      }
      return;
    }
    case 'steer':
    case 'follow_up': return respond(c, await queueInput(c));
    case 'clear_queue': {
      if (stopping) throw new Error('Session is stopping');
      inputEpoch++;
      const cleared = await editInputs(() => session.clearQueue());
      emitSettled();
      return respond(c, cleared);
    }
    case 'abort': {
      if (aborting) { await aborting; return respond(c); }
      stopping = true;
      inputEpoch++;
      aborting = (async () => {
        // Clear again after in-flight preflight, so /stop cannot leave a late input.
        session.clearQueue();
        try { await session.abort(); }
        finally {
          await inputTail;
          session.clearQueue(); stopping = false; aborting = undefined; emitSettled();
        }
      })();
      await aborting;
      return respond(c);
    }
    case 'compact':
      if (busy || compacting || stopping || inputCount) throw new Error('Session is busy');
      compacting = true;
      try { return respond(c, await session.compact(c.customInstructions)); }
      finally { compacting = false; }
    case 'switch_session': {
      if (busy || compacting || stopping || inputCount || pending.size || session.pendingMessageCount)
        throw new Error('Session is busy');
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
emit({ type: 'ready', engine: 'pi', version: engineVersion });
