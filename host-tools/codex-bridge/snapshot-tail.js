// Select a small conversation tail directly from IPC bytes. The desktop protocol
// sends full snapshots; do not turn images/tool history into a second JS heap copy.
'use strict';
const MAX_SMALL_MESSAGE = 1024 * 1024;
const TAIL_TURNS = 6;
const TAIL_MESSAGES = 8;

class JsonView {
  constructor(bytes) { this.bytes = bytes; }
  space(at) {
    while ([9, 10, 13, 32].includes(this.bytes[at])) at++;
    return at;
  }
  stringEnd(at) {
    const b = this.bytes;
    for (let end = b.indexOf(34, at + 1); end >= 0; end = b.indexOf(34, end + 1)) {
      let slash = end;
      while (slash > at && b[slash - 1] === 92) slash--;
      if ((end - slash) % 2 === 0) return end + 1;
    }
    throw Error('Incomplete IPC JSON string');
  }
  end(at) {
    const b = this.bytes;
    if (b[at] === 34) return this.stringEnd(at);
    if (b[at] !== 123 && b[at] !== 91) {
      let end = at;
      while (end < b.length && ![9, 10, 13, 32, 44, 93, 125].includes(b[end])) end++;
      if (end === at) throw Error('Invalid IPC JSON value');
      return end;
    }
    let depth = 0;
    for (let pos = at; pos < b.length; pos++) {
      const byte = b[pos];
      if (byte === 34) pos = this.stringEnd(pos) - 1;
      else if (byte === 123 || byte === 91) {
        if (++depth > 128) throw Error('IPC JSON nesting exceeds limit');
      } else if ((byte === 125 || byte === 93) && --depth === 0) return pos + 1;
    }
    throw Error('Incomplete IPC JSON value');
  }
  object(span) {
    const fields = new Map();
    if (!span || this.bytes[span[0]] !== 123) return fields;
    let at = this.space(span[0] + 1);
    while (this.bytes[at] !== 125) {
      if (this.bytes[at] !== 34) throw Error('Invalid IPC JSON key');
      const end = this.stringEnd(at), key = this.value([at, end], 4096);
      at = this.space(end);
      if (this.bytes[at++] !== 58) throw Error('Invalid IPC JSON member');
      at = this.space(at);
      const stop = this.end(at);
      fields.set(key, [at, stop]);
      at = this.space(stop);
      if (this.bytes[at] !== 44) break;
      at = this.space(at + 1);
    }
    if (this.bytes[at] !== 125 || at + 1 !== span[1]) throw Error('Invalid IPC JSON object');
    return fields;
  }
  array(span) {
    const entries = [];
    if (!span || this.bytes[span[0]] !== 91) return entries;
    let at = this.space(span[0] + 1);
    while (this.bytes[at] !== 93) {
      const end = this.end(at);
      entries.push([at, end]);
      at = this.space(end);
      if (this.bytes[at] !== 44) break;
      at = this.space(at + 1);
    }
    if (this.bytes[at] !== 93 || at + 1 !== span[1]) throw Error('Invalid IPC JSON array');
    return entries;
  }
  value(span, limit = 2048, tail = false) {
    if (!span) return undefined;
    const b = this.bytes, [start, end] = span;
    if (b[start] !== 34) {
      if (end - start > 256) throw Error('Unexpected non-scalar IPC field');
      return JSON.parse(b.toString('utf8', start, end));
    }
    // JSON strings can use six bytes per escaped character. Never decode an
    // unbounded text field, even inside one of the selected recent messages.
    let left = start + 1, right = end - 1;
    const escaped = at => {
      let p = at;
      while (p > start && b[p - 1] === 92) p--;
      return (at - p) % 2 === 1;
    };
    if (right - left > limit * 6) {
      if (tail) {
        left = right - limit * 6;
        while ((b[left] & 192) === 128) left++;
        // Do not begin inside a backslash escape or a \uXXXX sequence.
        for (let p = Math.max(start + 1, left - 5); p < left; p++) {
          if (b[p] === 92 && !escaped(p)) {
            const stop = p + (b[p + 1] === 117 ? 6 : 2);
            if (stop > left) left = stop;
          }
        }
      } else {
        right = left + limit * 6;
        while ((b[right] & 192) === 128) right--;
        for (let p = Math.max(left, right - 5); p < right; p++) {
          if (b[p] === 92 && !escaped(p)
            && p + (b[p + 1] === 117 ? 6 : 2) > right) { right = p; break; }
        }
      }
    }
    const text = JSON.parse('"' + b.toString('utf8', left, right) + '"');
    return tail ? text.slice(-limit) : text.slice(0, limit);
  }
  fields(span, names) {
    const fields = this.object(span), result = {};
    for (const name of names) if (fields.has(name)) result[name] = this.value(fields.get(name));
    return result;
  }
}

function textContent(view, span) {
  return view.array(span).flatMap(part => {
    const f = view.object(part);
    return view.value(f.get('type')) === 'text'
      ? [{type:'text', text:view.value(f.get('text'), 2001) || ''}] : [];
  }).slice(-8);
}

function turnTail(view, span, includeText) {
  const f = view.object(span);
  const turn = view.fields(span, ['turnId','status','turnStartedAtMs','durationMs']);
  if (includeText) {
    const params = view.object(f.get('params'));
    turn.params = {input:textContent(view, params.get('input'))};
  }
  const error = view.object(f.get('error'));
  turn.error = {message:view.value(error.get('message'), 500)};
  const items = view.array(f.get('items')).map(span => {
    const fields = view.object(span);
    return {span, fields, type:view.value(fields.get('type'))};
  });
  const visible = includeText
    ? items.filter(i => ['userMessage','agentMessage'].includes(i.type)).slice(-TAIL_MESSAGES) : [];
  const tools = items.filter(i => !['userMessage','agentMessage','reasoning'].includes(i.type)).slice(-6);
  const selected = new Set([...visible, ...tools, ...items.slice(-1)]);
  turn.items = items.filter(i => selected.has(i)).map(({span, fields, type}) => {
    const item = view.fields(span, ['type','id','phase','status','tool','toolName','server','exitCode']);
    if (includeText && type === 'agentMessage') item.text = view.value(fields.get('text'), 12001, true) || '';
    if (includeText && type === 'userMessage') item.content = textContent(view, fields.get('content'));
    return item;
  });
  return turn;
}

function stateTail(view, span, includeText) {
  const f = view.object(span);
  const state = view.fields(span, ['id','title','cwd','latestModel','latestReasoningEffort','daybreakEnabled','updatedAt']);
  const collaboration = view.object(f.get('latestCollaborationMode'));
  state.latestCollaborationMode = {settings:view.fields(collaboration.get('settings'), ['model'])};
  state.threadRuntimeStatus = view.fields(f.get('threadRuntimeStatus'), ['type']);
  const runtime = view.object(f.get('threadRuntimeStatus'));
  state.threadRuntimeStatus.activeFlags = view.array(runtime.get('activeFlags')).map(p => view.value(p)).slice(0, 16);
  let questionBudget = 12000;
  state.requests = view.array(f.get('requests')).slice(0, 8).map(p => {
    const request = view.fields(p, ['id','requestId','method','type']);
    // Only ordinary questions have remotely answerable data. Never copy command
    // arguments, permission payloads, elicitation tokens or arbitrary tool input.
    if ((request.method ?? request.type) === 'item/tool/requestUserInput') {
      if (!questionBudget) { request.truncated = true; return request; }
      const fields = view.object(p), params = view.object(fields.get('params'));
      const questions = view.array(params.get('questions'));
      request.truncated = questions.length > 8;
      const text = (span, limit) => {
        const value = view.value(span, limit + 1);
        if (typeof value !== 'string') return value;
        const room = Math.min(limit, questionBudget);
        if (value.length > room) request.truncated = true;
        const result = value.slice(0, room); questionBudget -= result.length;
        return result;
      };
      request.params = {turnId:view.value(params.get('turnId')), questions:questions.slice(0, 8).map(q => {
        const fields = view.object(q), options = view.array(fields.get('options'));
        return {...view.fields(q, ['isSecret','isOther']),
          id:text(fields.get('id'), 200), header:text(fields.get('header'), 200),
          question:text(fields.get('question'), 2000),
          options:options.slice(0, 8).map(o => {
            const fields = view.object(o);
            return {label:text(fields.get('label'), 300), description:text(fields.get('description'), 500)};
          }),
          truncated:options.length > 8};
      })};
    }
    return request;
  });
  const history = view.object(f.get('turnHistory'));
  let spans;
  if (view.value(history.get('kind')) === 'canonical') {
    const h = view.object(history.get('history')), entities = view.object(h.get('entitiesByKey'));
    spans = view.array(h.get('islands')).flatMap(island => {
      const i = view.object(island);
      return view.array(i.get('entries')).map(entry => {
        const e = view.object(entry);
        return entities.get(view.value(e.get('value')));
      }).filter(Boolean);
    });
  } else spans = view.array(f.get('turns'));
  const recent = new Set(spans.slice(includeText ? -TAIL_TURNS : -1));
  // Preserve an active turn even if a paginated history island follows it.
  const active = spans.findLast(p => view.value(view.object(p).get('status')) === 'inProgress');
  if (active) recent.add(active);
  state.turns = spans.filter(p => recent.has(p)).map(p => turnTail(view, p, includeText));
  return state;
}

function decodeMessage(bytes, includeTextForThread = () => true) {
  const view = new JsonView(bytes), start = view.space(0), end = view.end(start);
  if (view.space(end) !== bytes.length) throw Error('Trailing IPC JSON content');
  const root = view.object([start, end]);
  if (view.value(root.get('type')) !== 'broadcast'
    || view.value(root.get('method')) !== 'thread-stream-state-changed') {
    if (bytes.length > MAX_SMALL_MESSAGE) throw Error('Non-snapshot IPC message exceeds limit');
    return JSON.parse(bytes.toString('utf8'));
  }
  const message = view.fields([start, end], ['type','method','version','sourceClientId']);
  const params = view.object(root.get('params')), change = view.object(params.get('change'));
  message.params = view.fields(root.get('params'), ['hostId','conversationId']);
  message.params.change = view.fields(params.get('change'), ['type']);
  // This client only requests a current snapshot. Unsubscribe after it arrives;
  // queued patches need neither parsing nor retention.
  if (message.params.change.type === 'snapshot') {
    message.params.change.conversationState = stateTail(view, change.get('conversationState'),
      includeTextForThread(message.params.conversationId));
  }
  return message;
}
module.exports = {decodeMessage};
