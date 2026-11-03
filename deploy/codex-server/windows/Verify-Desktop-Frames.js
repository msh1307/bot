// Exercise real framing with synthetic data; never connect to a user conversation.
'use strict';
const assert = require('assert/strict');
const {DesktopIPC} = require('./desktop-ipc');
const {performance} = require('perf_hooks');

const ipc = new DesktopIPC();
const socket = {destroyed: false, destroy() {this.destroyed = true;}, write() {}};
ipc.socket = socket;
const received = [];
ipc.receive = message => received.push(message);
const started = performance.now();

// Exact frame size from the failed Discord send, without real conversation data.
const frameBytes = 152592455;
const start = Buffer.from('{"type":"broadcast","method":"thread-stream-state-changed","version":11,'
  + '"params":{"hostId":"local","conversationId":"verification","change":{"type":"snapshot",'
  + '"conversationState":{"id":"verification","turns":[{"turnId":"turn-1","status":"inProgress",'
  + '"items":[{"type":"commandExecution","output":"');
const end = Buffer.from('"}]}],"title":"한글"}}}}');
const initialHeap = process.memoryUsage().heapUsed;
const header = Buffer.alloc(4); header.writeUInt32LE(frameBytes);
ipc.consume(socket, header.subarray(0, 1));
ipc.consume(socket, header.subarray(1));
ipc.consume(socket, start);
const chunk = Buffer.alloc(65536, 120);
for (let left = frameBytes - start.length - end.length; left > 0;) {
  const count = Math.min(chunk.length, left);
  ipc.consume(socket, chunk.subarray(0, count));
  left -= count;
}
// Coalesced next header, split UTF-8, and two frames in one read.
const next = Buffer.from('{"type":"following"}');
const nextHeader = Buffer.alloc(4); nextHeader.writeUInt32LE(next.length);
ipc.consume(socket, end.subarray(0, end.length - 4));
ipc.consume(socket, Buffer.concat([end.subarray(end.length - 4), nextHeader, next]));
assert.equal(socket.destroyed, false);
assert.equal(ipc.lastFault, null);
assert.equal(received.length, 2);
const state = received[0].params.change.conversationState;
assert.equal(state.title, '한글');
assert.equal(state.turns[0].turnId, 'turn-1');
assert.equal(state.turns[0].items[0].output, undefined);
const decodedBytes = Buffer.byteLength(JSON.stringify(received[0]));
assert(decodedBytes < 2048);
const heapGrowth = process.memoryUsage().heapUsed - initialHeap;
assert(heapGrowth < 32 * 1024 * 1024, 'Snapshot payload was expanded on the JS heap');
assert.equal(received[1].type, 'following');
assert.equal(ipc.frame, null); // No retained raw snapshot buffer.
received.length = 0;

// Lifting the inbound limit must not lift the outbound command limit.
assert.throws(() => ipc.write({text:'x'.repeat(1024 * 1024)}), {code:'request_too_large'});
// Reject an oversized header before allocating its body.
header.writeUInt32LE(512 * 1024 * 1024 + 1);
ipc.consume(socket, header);
assert.equal(ipc.lastFault?.code, 'snapshot_too_large');
assert.equal(socket.destroyed, true);
assert.equal(ipc.frame, null);
console.log(JSON.stringify({passed:true,frame_bytes:frameBytes,
  decoded_bytes:decodedBytes, heap_growth_bytes:heapGrowth,
  checks:['original_failure_size','tool_output_discarded','bounded_heap','fragmented_header','fragmented_body','coalesced_frames',
    'utf8','raw_buffer_released','outbound_limit','oversized_header_rejected'],
  elapsed_ms:Math.round(performance.now()-started), real_gui_mutations:false}));
