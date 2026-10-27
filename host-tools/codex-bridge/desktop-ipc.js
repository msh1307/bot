// Adapted to the installed desktop IPC protocol, not a public app-server API.
// Requests follow the GUI owner; unopened chats are loaded by the app itself.
'use strict';
const net = require('net');
const crypto = require('crypto');
const {decodeMessage} = require('./snapshot-tail');
const PIPE = '\\\\.\\pipe\\codex-ipc';
// A GUI snapshot includes the thread's images/tool history, not only visible text.
// Real, long-running GUI threads exceed 128 MiB before send can inspect the turn.
// This inbound limit is separate from small commands sent to the desktop.
const MAX_FRAME = 512 * 1024 * 1024;
const MAX_REQUEST = 1024 * 1024;
const THREAD_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

class DesktopError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

function turnsOf(state) {
  const history = state.turnHistory?.kind === 'canonical' ? state.turnHistory.history : null;
  if (!history) return state.turns || [];
  return (history.islands || []).flatMap(island => (island.entries || [])
    .map(entry => history.entitiesByKey?.[entry.value]).filter(Boolean));
}

function recentMessages(turns) {
  const messages = [];
  for (const turn of turns.slice(-6)) {
    let hasUser = false;
    for (const item of turn.items || []) {
      if (item.type !== 'userMessage' && item.type !== 'agentMessage') continue;
      const text = item.type === 'agentMessage' ? item.text
        : (item.content || []).filter(c => c.type === 'text').map(c => c.text || '').join('\n');
      if (typeof text !== 'string' || !text) continue;
      if (item.type === 'userMessage') hasUser = true;
      messages.push({turn_id: turn.turnId || null, role: item.type === 'userMessage' ? 'user' : 'assistant',
        phase: item.phase || null, text: text.slice(0, 2000), truncated: text.length > 2000});
    }
    if (!hasUser) {
      const text = (turn.params?.input || []).filter(c => c.type === 'text').map(c => c.text || '').join('\n');
      if (text) {
        const first = messages.findIndex(m => m.turn_id === turn.turnId);
        const user = {turn_id: turn.turnId || null, role: 'user', phase: null,
          text: text.slice(0, 2000), truncated: text.length > 2000};
        if (first >= 0) messages.splice(first, 0, user); else messages.push(user);
      }
    }
  }
  // Only visible user/assistant messages. Internal reasoning and tool arguments are excluded.
  const latestUser = messages.findLast(m => m.role === 'user');
  let selected = messages.slice(-8);
  if (latestUser && !selected.includes(latestUser)) selected = [latestUser, ...selected.slice(-7)];
  let budget = 6000, reserved = latestUser?.text.length || 0;
  const result = [];
  for (const message of selected.reverse()) {
    if (budget <= 0) break;
    const available = message === latestUser ? budget : Math.max(0, budget - reserved);
    if (!available) continue;
    const text = message.text.slice(0, available);
    result.unshift({...message, text, truncated: message.truncated || text.length < message.text.length});
    budget -= text.length;
    if (message === latestUser) reserved = 0;
  }
  return result;
}

function summarize(state, includeAnswer = true) {
  const turns = turnsOf(state);
  const turn = turns.findLast(t => t.turnId && t.status === 'inProgress')
    || turns.findLast(t => t.turnId) || turns.at(-1);
  const items = turn?.items || [];
  const messages = items.filter(i => i.type === 'agentMessage' && typeof i.text === 'string');
  const final = messages.filter(i => i.phase === 'final_answer');
  const runtime = state.threadRuntimeStatus?.type || 'unknown';
  const active = turn?.status === 'inProgress';
  const runtimeFlags = (state.threadRuntimeStatus?.activeFlags || [])
    .filter(flag => ['waitingOnApproval', 'waitingOnUserInput'].includes(flag));
  const requests = (state.requests || []).slice(0, 8).map(r => {
    const method = r.method ?? r.type;
    const questions = method === 'item/tool/requestUserInput' ? r.params?.questions || [] : [];
    const answerable = questions.length > 0 && !r.truncated && questions.every(q =>
      typeof q.id === 'string' && q.id.length <= 200 && !q.isSecret && !q.truncated
      && typeof q.question === 'string' && q.question.length <= 2000);
    return {request_id:r.id ?? r.requestId, method, answerable,
      ...(questions.length ? {turn_id:r.params?.turnId, questions} : {}),
      message:answerable ? 'Use answer with this request_id and answers keyed by question ID.'
        : 'Respond in the desktop app; this bridge does not resolve approvals or secret input.'};
  });
  const turnError = typeof turn?.error?.message === 'string' ? turn.error.message.slice(0, 500) : null;
  const approval = runtimeFlags.includes('waitingOnApproval')
    || requests.some(r => /approval/i.test(r.method || ''));
  const input = runtimeFlags.includes('waitingOnUserInput')
    || requests.some(r => /requestUserInput|elicitation/i.test(r.method || ''));
  const attention = approval ? {kind: 'approval_required', reason: 'Waiting for approval in the desktop app.'}
    : input ? {kind: 'input_required', reason: 'Waiting for an answer in the desktop app.'}
    : requests.length ? {kind: 'request_pending', reason: 'A desktop request needs a response.'}
    : runtime === 'systemError' || turn?.status === 'failed'
      ? {kind: 'error', reason: turnError || 'The desktop reports a failed turn or system error.'} : null;
  const activity = items.filter(i => !['userMessage', 'agentMessage', 'reasoning'].includes(i.type))
    .slice(-6).map(i => ({type: i.type, item_id: i.id || null, status: i.status || null,
      tool: typeof i.tool === 'string' ? i.tool : typeof i.toolName === 'string' ? i.toolName : null,
      server: typeof i.server === 'string' ? i.server : null,
      exit_code: Number.isInteger(i.exitCode) ? i.exitCode : null}));
  return {
    thread_id: state.id, title: state.title, cwd: state.cwd,
    source: 'desktop', gui_attached: true,
    model: state.latestCollaborationMode?.settings?.model || state.latestModel,
    reasoning_effort: state.latestReasoningEffort, daybreak_enabled: state.daybreakEnabled,
    runtime_status: runtime, runtime_flags: runtimeFlags,
    needs_attention: Boolean(attention), attention, turn_id: turn?.turnId || null,
    turn_status: turn?.status || null,
    turn_started_at: turn?.turnStartedAtMs || null,
    phase: items.at(-1)?.type || runtime,
    elapsed_seconds: active && turn?.turnStartedAtMs
      ? Math.max(0, Math.floor((Date.now() - turn.turnStartedAtMs) / 1000))
      : typeof turn?.durationMs === 'number' ? Math.floor(turn.durationMs / 1000) : null,
    pending_requests: requests,
    ...(includeAnswer ? {answer: (final.length ? final.map(i => i.text).join('\n\n')
      : messages.at(-1)?.text || '').slice(-12000), answer_is_partial: active,
      recent_messages: recentMessages(turns), activity,
      conversation_view: {source: 'live_gui_snapshot_tail', complete_history: false,
        description: 'Recent visible conversation window; an empty answer can mean tools are running, not unreadable history.'},
      turn_error: turnError} : {}),
    updated_at: state.updatedAt,
  };
}

class DesktopIPC {
  constructor(onDiagnostic = () => {}, openThread = null) {
    this.socket = null; this.connecting = null; this.clientId = '';
    this.header = Buffer.alloc(4); this.headerBytes = 0;
    this.frame = null; this.frameBytes = 0;
    this.pending = new Map(); this.snapshots = new Map();
    this.onDiagnostic = onDiagnostic; this.lastFault = null;
    this.openThread = openThread;
  }
  close() {
    const socket = this.socket;
    if (socket) { this.fail(socket); socket.destroy(); }
  }
  fail(socket, fault = null) {
    if (socket !== this.socket) return;
    this.socket = null; this.clientId = '';
    this.headerBytes = 0; this.frame = null; this.frameBytes = 0;
    if (fault) {
      this.lastFault = {at: new Date().toISOString(), ...fault};
      this.onDiagnostic(this.lastFault);
    }
    for (const p of this.pending.values()) p.reject(new DesktopError('outcome_unknown',
      'Desktop IPC disconnected. Check status before retrying a command.'));
    this.pending.clear();
    for (const p of this.snapshots.values()) p.reject(new DesktopError(
      fault?.code || 'desktop_unavailable', fault?.code === 'snapshot_too_large'
        ? `Desktop snapshot is ${fault.frame_bytes} bytes; bridge limit is ${MAX_FRAME} bytes.`
        : 'Desktop IPC disconnected. Retry status; no command was resubmitted.'));
    this.snapshots.clear();
  }
  async connect() {
    if (this.socket && this.clientId) return;
    if (this.connecting) return this.connecting;
    this.connecting = (async () => {
      const socket = net.connect(PIPE);
      this.socket = socket; this.headerBytes = 0; this.frame = null; this.frameBytes = 0;
      socket.on('error', () => this.fail(socket, {code: 'ipc_socket_error'}));
      socket.on('close', () => this.fail(socket, {code: 'ipc_socket_closed'}));
      socket.on('data', chunk => this.consume(socket, chunk));
      try {
        await new Promise((resolve, reject) => {
          const timer = setTimeout(() => { socket.destroy(); reject(new DesktopError(
            'desktop_unavailable', 'Codex desktop IPC is unavailable. Open the desktop app.')); }, 5000);
          socket.once('connect', () => { clearTimeout(timer); resolve(); });
          socket.once('error', () => { clearTimeout(timer); reject(new DesktopError(
            'desktop_unavailable', 'Codex desktop IPC is unavailable. Open the desktop app.')); });
        });
        const reply = await this.request('initialize', {clientType: 'evo-discord-desktop'}, 0, null, true);
        if (socket !== this.socket || socket.destroyed) throw new DesktopError(
          'desktop_unavailable', 'Desktop IPC disconnected during initialization.');
        if (typeof reply.result?.clientId !== 'string') throw new DesktopError(
          'protocol_mismatch', 'Desktop IPC initialization changed; bridge update required.');
        this.clientId = reply.result.clientId;
      } catch (error) { socket.destroy(); throw error; }
    })();
    try { await this.connecting; } finally { this.connecting = null; }
  }
  write(message) {
    if (!this.socket || this.socket.destroyed) throw new DesktopError('desktop_unavailable',
      'Codex desktop IPC is disconnected.');
    const body = Buffer.from(JSON.stringify(message));
    if (body.length > MAX_REQUEST) throw new DesktopError('request_too_large', 'IPC request too large.');
    const header = Buffer.alloc(4); header.writeUInt32LE(body.length);
    this.socket.write(Buffer.concat([header, body]));
  }
  consume(socket, chunk) {
    if (socket !== this.socket) return;
    try {
      let offset = 0;
      while (offset < chunk.length) {
        if (!this.frame) {
          const count = Math.min(4 - this.headerBytes, chunk.length - offset);
          chunk.copy(this.header, this.headerBytes, offset, offset + count);
          this.headerBytes += count; offset += count;
          if (this.headerBytes < 4) return;
          const length = this.header.readUInt32LE(0);
          if (!length || length > MAX_FRAME) {
            this.fail(socket, {code: length ? 'snapshot_too_large' : 'ipc_invalid_frame', frame_bytes: length});
            socket.destroy(); return;
          }
          // Allocate once; repeated Buffer.concat copied large snapshots quadratically.
          this.frame = Buffer.allocUnsafe(length); this.frameBytes = 0; this.headerBytes = 0;
        }
        const count = Math.min(this.frame.length - this.frameBytes, chunk.length - offset);
        chunk.copy(this.frame, this.frameBytes, offset, offset + count);
        this.frameBytes += count; offset += count;
        if (this.frameBytes < this.frame.length) return;
        const frame = this.frame;
        this.frame = null; this.frameBytes = 0;
        const message = decodeMessage(frame, id => this.snapshots.get(id)?.includeText !== false);
        this.receive(message);
      }
    } catch (_) { this.fail(socket, {code: 'ipc_decode_error'}); socket.destroy(); }
  }
  receive(message) {
    if (message.type === 'response') {
      const pending = this.pending.get(message.requestId);
      if (!pending) return;
      if (message.resultType === 'success') pending.resolve(message);
      else pending.reject(new DesktopError('desktop_rejected', typeof message.error === 'string'
        ? message.error.slice(0, 300) : 'Desktop rejected the request.'));
    } else if (message.type === 'client-discovery-request') {
      this.write({type: 'client-discovery-response', requestId: message.requestId,
        response: {canHandle: false}});
    } else if (message.type === 'request') {
      this.write({type: 'response', requestId: message.requestId,
        resultType: 'error', error: 'no-handler-for-request'});
    } else if (message.type === 'broadcast' && message.method === 'thread-stream-state-changed'
      && message.version === 11 && message.params?.hostId === 'local'
      && message.params?.change?.type === 'snapshot') {
      const pending = this.snapshots.get(message.params.conversationId);
      if (pending && pending.owner === message.sourceClientId) {
        pending.resolve(message.params.change.conversationState);
      }
    }
  }
  async request(method, params, version, targetClientId, initializing = false) {
    if (!initializing) await this.connect();
    if (this.pending.size >= 24) throw new DesktopError('busy', 'Desktop bridge is busy.');
    const requestId = crypto.randomUUID();
    let timer;
    try {
      return await new Promise((resolve, reject) => {
        this.pending.set(requestId, {resolve, reject});
        timer = setTimeout(() => reject(new DesktopError('outcome_unknown',
          'Desktop IPC request timed out. Check status before retrying; no automatic resubmission.')), 7000);
        this.write({type: 'request', requestId, sourceClientId: this.clientId,
          version, method, params, timeoutMs: 6000, ...(targetClientId ? {targetClientId} : {})});
      });
    } finally { clearTimeout(timer); this.pending.delete(requestId); }
  }
  async owner(threadId) {
    if (!THREAD_ID.test(threadId || '')) throw new DesktopError('invalid_thread', 'Use an exact thread_id from list.');
    let reply;
    try { reply = await this.request('thread-owner-discovery',
      {hostId: 'local', conversationId: threadId}, 1); }
    catch (error) {
      if (error.code !== 'desktop_rejected' || !/no-client-found/.test(error.message)) throw error;
      throw new DesktopError('no_gui_owner', 'This thread has no live desktop owner. Open it in the Codex app. It was not resumed on another server.');
    }
    if (!reply.handledByClientId) throw new DesktopError('no_gui_owner',
      'This thread has no live desktop owner. Open it in the Codex app. It was not resumed on another server.');
    return reply.handledByClientId;
  }
  following(threadId, owner, following) {
    this.write({type: 'broadcast', method: 'thread-stream-following-changed',
      sourceClientId: this.clientId, version: 1, targetClientIds: [owner],
      params: {hostId: 'local', conversationId: threadId, following}});
  }
  async state(threadId, owner, includeText = true) {
    const current = this.snapshots.get(threadId);
    if (current) {
      if (current.owner !== owner) throw new DesktopError('owner_changed', 'GUI owner changed; retry status.');
      // A simultaneous read must not reuse a metadata-only control response.
      if (includeText && !current.includeText) {
        await current.promise;
        return this.state(threadId, owner, true);
      }
      return current.promise;
    }
    const socket = this.socket, clientId = this.clientId;
    const entry = {owner, includeText};
    const response = new Promise((resolve, reject) => Object.assign(entry, {resolve, reject}));
    const timer = setTimeout(() => entry.reject(new DesktopError('snapshot_unavailable',
      'No GUI state snapshot received; desktop protocol may have changed.')), 12000);
    entry.promise = response.finally(() => {
      clearTimeout(timer);
      if (this.snapshots.get(threadId) === entry) this.snapshots.delete(threadId);
      if (this.socket === socket && this.clientId === clientId && !socket?.destroyed) {
        try { this.following(threadId, owner, false); } catch (_) { /* Connection already closed. */ }
      }
    });
    this.snapshots.set(threadId, entry);
    try { this.following(threadId, owner, true); } catch (error) { entry.reject(error); }
    return entry.promise;
  }
  async action(args) {
    if (args.action === 'read') args = {...args, action: 'status'};
    if (args.action === 'open') {
      const id = args.thread_id;
      try { return summarize(await this.state(id, await this.owner(id), false), false); }
      catch (error) { if (error.code !== 'no_gui_owner') throw error; }
      if (!this.openThread) throw new DesktopError('open_unavailable', 'Desktop chat loading is unavailable.');
      await this.openThread(id);
      const deadline = Date.now() + 15000;
      do {
        try { return summarize(await this.state(id, await this.owner(id), false), false); }
        catch (error) { if (error.code !== 'no_gui_owner') throw error; }
        await new Promise(resolve => setTimeout(resolve, 500));
      } while (Date.now() < deadline);
      throw new DesktopError('open_pending', 'The app was asked to load this chat; no message was sent. Check status before retrying.');
    }
    if (args.action === 'inspect') {
      if (!Array.isArray(args.thread_ids) || args.thread_ids.length > 12) throw new DesktopError(
        'invalid_request', 'Inspect accepts at most 12 thread IDs.');
      const threads = [];
      const deadline = Date.now() + 18000;
      for (const id of [...new Set(args.thread_ids)]) {
        if (Date.now() >= deadline) { threads.push({thread_id: id, gui_attached: false,
          error_code: 'inspection_deadline', error: 'Not inspected within the list deadline; use status.'}); continue; }
        try {
          const owner = await this.owner(id);
          threads.push(summarize(await this.state(id, owner, false), false));
        } catch (error) {
          if (['desktop_unavailable', 'outcome_unknown', 'protocol_mismatch'].includes(error.code)) throw error;
          threads.push({thread_id: id, gui_attached: false, error_code: error.code, error: error.message});
        }
      }
      return {threads};
    }
    if (!['status', 'send', 'answer', 'stop'].includes(args.action)) throw new DesktopError('invalid_request', 'Use inspect/status/send/answer/stop.');
    const id = args.thread_id, owner = await this.owner(id);
    const state = summarize(await this.state(id, owner, args.action === 'status'), args.action === 'status');
    if (args.action === 'status') return state;
    if (args.action === 'answer') {
      const pending = state.pending_requests.find(r => String(r.request_id) === String(args.request_id));
      if (args.request_id == null || !pending) throw new DesktopError('stale_request',
        'This request is no longer pending. Read status before answering.');
      if (!pending.answerable) throw new DesktopError('owner_input_required',
        'This is an approval, secret or unsupported question. The owner must respond in the desktop app.');
      if (pending.turn_id && pending.turn_id !== state.turn_id) throw new DesktopError('stale_request',
        'The question belongs to a different turn. Read status again.');
      const answers = args.answers;
      if (!answers || typeof answers !== 'object' || Array.isArray(answers)
        || Object.keys(answers).length !== pending.questions.length
        || pending.questions.some(q => !Object.hasOwn(answers, q.id)
          || !Array.isArray(answers[q.id]) || !answers[q.id].length || answers[q.id].length > 8
          || answers[q.id].some(a => typeof a !== 'string' || !a.trim() || a.length > 2000))) {
        throw new DesktopError('invalid_answer', 'Answer every question ID using an array of nonempty strings (at most 2000 characters each).');
      }
      const response = {answers:Object.fromEntries(pending.questions.map(q => [q.id, {answers:answers[q.id]}]))};
      await this.request('thread-follower-submit-user-input', {
        conversationId:id, requestId:pending.request_id, response}, 1, owner);
      return {thread_id:id, source:'desktop', turn_id:state.turn_id,
        request_id:pending.request_id, mode:'answer',
        message:'Answer submitted to this GUI question. Check status for confirmation; do not resubmit blindly.'};
    }
    if (args.action === 'stop') {
      if (state.turn_status !== 'inProgress' || !state.turn_id) return {...state, message: 'No active GUI turn to interrupt.'};
      const reply = await this.request('thread-follower-interrupt-turn', {
        conversationId: id, mode: 'user-stop', expectedTurnId: state.turn_id}, 4, owner);
      return {thread_id: id, source: 'desktop', interrupted_turn_id: reply.result?.interruptedTurnId,
        message: 'Interrupt sent to this GUI thread. Check status for confirmation.'};
    }
    if (typeof args.prompt !== 'string' || args.prompt.length < 1 || args.prompt.length > 16000) throw new DesktopError(
      'invalid_prompt', 'Provide prompt (1–16000 characters).');
    if (state.pending_requests.length || state.runtime_flags.length) throw new DesktopError('waiting_for_input',
      'This GUI thread is waiting for approval/input. Respond in the desktop app first.');
    const clientUserMessageId = crypto.randomUUID();
    const input = [{type: 'text', text: args.prompt}];
    const active = state.turn_status === 'inProgress';
    const method = active ? 'thread-follower-steer-turn' : 'thread-follower-start-turn';
    const params = active ? {conversationId: id, input, clientUserMessageId,
      // The GUI uses this local draft context to render/recover a steering message.
      // Omit history and inherit the active conversation's settings in the owner.
      restoreMessage: {id: clientUserMessageId, input, cwd: state.cwd,
        context: {attachments: [], commentAttachments: []}}} : {
      conversationId: id, turnStart: {
        request: {threadId: id, input, clientUserMessageId},
        context: {inheritThreadSettings: true, attachments: [], commentAttachments: []}
      }
    };
    const reply = await this.request(method, params, active ? 1 : 2, owner);
    return {thread_id: id, source: 'desktop', mode: active ? 'steer' : 'start_turn',
      previous_turn_id: state.turn_id,
      turn_id: reply.result?.result?.turn?.id || reply.result?.result?.turnId || (active ? state.turn_id : null),
      message: 'Instruction accepted by the existing GUI owner. Use status for progress/result.'};
  }
}
module.exports = {DesktopIPC, DesktopError};
