// Verify GUI request shaping in isolation, using the installed IPC envelope.
// Real GUI list/read and reply envelopes are checked separately; no user turn is mutated.
'use strict';
const assert = require('assert/strict');
const {DesktopIPC, DesktopError} = require('./desktop-ipc');
const threadId = '00000000-0000-4000-8000-000000000001';
const ownerId = 'verification-owner';
const calls = [];
function state(status = 'completed', requests = []) {
  return {id:threadId, title:'verification', cwd:'C:\\verification', requests,
    latestModel:'verification-model', threadRuntimeStatus:{type:status === 'inProgress' ? 'active' : 'idle'},
    turns:[{turnId:'verification-turn',status,items:[]}]};
}
function client(current, answer = null) {
  const ipc = new DesktopIPC();
  ipc.owner = async id => {assert.equal(id,threadId); return ownerId;};
  ipc.state = async (id,owner) => {assert.equal(id,threadId);assert.equal(owner,ownerId);return current;};
  ipc.request = async (method,params,version,target) => {
    calls.push({method,params,version,target});
    assert.equal(target,ownerId);
    return answer || {result:{result:{turn:{id:'verification-next-turn'}}}};
  };
  return ipc;
}
(async () => {
  let ipc = client(state());
  let result = await ipc.action({action:'send',thread_id:threadId,prompt:'verification only'});
  let call = calls.at(-1);
  assert.equal(call.method,'thread-follower-start-turn');assert.equal(call.version,2);
  assert.equal(call.params.turnStart.request.threadId,threadId);
  assert.equal(call.params.turnStart.context.inheritThreadSettings,true);
  assert.equal(result.turn_id,'verification-next-turn');assert.equal(result.mode,'start_turn');
  assert(!('model' in call.params.turnStart.request));assert(!('sandboxPolicy' in call.params.turnStart.request));

  ipc = client(state('inProgress'),{result:{result:{turnId:'verification-turn'}}});
  result = await ipc.action({action:'send',thread_id:threadId,prompt:'verification steer'});
  call = calls.at(-1);
  assert.equal(call.method,'thread-follower-steer-turn');assert.equal(call.version,1);
  assert.equal(call.params.conversationId,threadId);assert.equal(result.mode,'steer');
  assert.equal(result.turn_id,'verification-turn');
  assert.equal(call.params.restoreMessage.id,call.params.clientUserMessageId);
  assert.deepEqual(call.params.restoreMessage.context,{attachments:[],commentAttachments:[]});
  assert(!('turns' in call.params));

  ipc = client(state('inProgress'),{result:{interruptedTurnId:'verification-turn',ok:true}});
  result = await ipc.action({action:'stop',thread_id:threadId});call = calls.at(-1);
  assert.equal(call.method,'thread-follower-interrupt-turn');assert.equal(call.version,4);
  assert.equal(call.params.expectedTurnId,'verification-turn');assert.equal(call.params.mode,'user-stop');
  assert.equal(result.interrupted_turn_id,'verification-turn');

  const before = calls.length;
  await client(state()).action({action:'stop',thread_id:threadId});
  assert.equal(calls.length,before);

  const question = {id:17,method:'item/tool/requestUserInput',params:{turnId:'verification-turn',
    questions:[{id:'target',question:'Which target?',options:[{label:'staging',description:'Test environment'}]}]}};
  ipc = client(state('inProgress',[question]),{result:{ok:true}});
  const status = await ipc.action({action:'status',thread_id:threadId});
  assert.equal(status.pending_requests[0].answerable,true);
  assert.equal(status.pending_requests[0].questions[0].question,'Which target?');
  result = await ipc.action({action:'answer',thread_id:threadId,request_id:'17',answers:{target:['staging']}});
  call = calls.at(-1);
  assert.equal(call.method,'thread-follower-submit-user-input');assert.equal(call.version,1);
  assert.equal(call.params.requestId,17);
  assert.deepEqual(call.params.response,{answers:{target:{answers:['staging']}}});
  assert.equal(result.mode,'answer');
  const answered = calls.length;
  await assert.rejects(ipc.action({action:'answer',thread_id:threadId,request_id:18,
    answers:{target:['staging']}}),{code:'stale_request'});
  await assert.rejects(ipc.action({action:'answer',thread_id:threadId,request_id:17,
    answers:{wrong:['staging']}}),{code:'invalid_answer'});
  await assert.rejects(client(state('inProgress',[{id:18,method:'item/commandExecution/requestApproval'}]))
    .action({action:'answer',thread_id:threadId,request_id:18,answers:{target:['yes']}}),{code:'owner_input_required'});
  await assert.rejects(client(state('inProgress',[{...question,params:{...question.params,
    questions:[{id:'password',question:'Password?',isSecret:true}]}}]))
    .action({action:'answer',thread_id:threadId,request_id:17,answers:{password:['secret']}}),{code:'owner_input_required'});
  assert.equal(calls.length,answered);
  await assert.rejects(client(state('inProgress',[{id:1,method:'item/commandExecution/requestApproval'}]))
    .action({action:'send',thread_id:threadId,prompt:'blocked'}),{code:'waiting_for_input'});
  assert.equal(calls.length,answered);

  ipc = client(state());
  let attempts = 0;
  ipc.request = async () => {attempts++; const error=new Error('outcome unknown');error.code='outcome_unknown';throw error;};
  await assert.rejects(ipc.action({action:'send',thread_id:threadId,prompt:'verification'}),{code:'outcome_unknown'});
  assert.equal(attempts,1);
  ipc = new DesktopIPC();
  await assert.rejects(ipc.owner('not-a-thread-id'),{code:'invalid_thread'});
  let opened = 0, acquired = false;
  ipc = client(state());
  ipc.openThread = async id => {assert.equal(id,threadId);opened++;acquired=true;};
  ipc.owner = async () => {
    if (!acquired) throw new DesktopError('no_gui_owner','closed');
    return ownerId;
  };
  result = await ipc.action({action:'open',thread_id:threadId});
  assert(result.gui_attached);assert.equal(opened,1);
  await ipc.action({action:'open',thread_id:threadId});
  assert.equal(opened,1); // An already loaded chat never navigates again.
  acquired = false;
  ipc.owner = async () => {throw new DesktopError('outcome_unknown','disconnected');};
  await assert.rejects(ipc.action({action:'open',thread_id:threadId}),{code:'outcome_unknown'});
  assert.equal(opened,1);
  console.log(JSON.stringify({passed:true,checks:['idle_start_payload','settings_inheritance',
    'active_steer_payload','exact_turn_stop','idle_stop_noop','approval_block',
    'question_read','question_answer_payload','stale_answer_rejected','exact_question_ids',
    'approval_answer_rejected','secret_answer_rejected','timeout_no_retry','invalid_id',
    'closed_chat_load','loaded_chat_no_navigation','ambiguous_owner_no_navigation'],real_gui_mutations:false}));
})().catch(error => {console.error(error);process.exitCode=1;});
