'use strict';
const assert = require('assert/strict');
const {DesktopIPC, DesktopError} = require('./desktop-ipc');
const id = '00000000-0000-4000-8000-000000000001';
const owner = 'test-owner';
function client() {
  const ipc = new DesktopIPC(), calls = [];
  const current = {model:'model-a', reasoningEffort:'high', daybreakEnabled:true};
  const permissions = {approvalPolicy:'on-request', sandboxPolicy:{type:'workspaceWrite'}};
  ipc.owner = async value => {assert.equal(value,id);return owner;};
  ipc.state = async () => ({latestThreadSettings:permissions});
  ipc.request = async (method, params, version, target) => {
    calls.push({method,params,version,target});assert.equal(target,owner);
    assert.equal(params.conversationId,id);
    if (method === 'thread-follower-read-model-settings') {
      assert.equal(version,1);return {result:{settings:{...current}}};
    }
    if (method === 'thread-follower-update-daybreak') {
      assert.equal(version,1);current.daybreakEnabled=params.daybreakEnabled;
      return {result:{ok:true}};
    }
    assert.equal(method,'thread-follower-update-thread-settings');assert.equal(version,2);
    if ('model' in params.threadSettings) current.model=params.threadSettings.model;
    if ('approvalPolicy' in params.threadSettings) permissions.approvalPolicy=params.threadSettings.approvalPolicy;
    if ('sandboxPolicy' in params.threadSettings) permissions.sandboxPolicy=params.threadSettings.sandboxPolicy;
    return {result:{applied:true}};
  };
  return {ipc,calls,current,permissions};
}
(async () => {
  const fixture=client(), {ipc,calls}=fixture;
  let result=await ipc.action({action:'settings',thread_id:id});
  assert.equal(result.model,'model-a');assert.equal(result.daybreak_enabled,true);
  assert.equal(result.approval_policy,'on-request');assert.equal(result.sandbox,'workspaceWrite');
  await ipc.action({action:'update_settings',thread_id:id,thread_settings:{model:'model-a'},daybreak_enabled:true});
  assert(calls.every(c=>c.method==='thread-follower-read-model-settings'));
  result=await ipc.action({action:'update_settings',thread_id:id,daybreak_enabled:false});
  assert.equal(result.daybreak_enabled,false);assert.equal(result.model,'model-a');
  assert(!calls.some(c=>c.method==='thread-follower-update-thread-settings'));
  result=await ipc.action({action:'update_settings',thread_id:id,thread_settings:{sandboxPolicy:{type:'readOnly'}}});
  const update=calls.find(c=>c.method==='thread-follower-update-thread-settings');
  assert.deepEqual(update.params,{conversationId:id,threadSettings:{sandboxPolicy:{type:'readOnly'}}});
  assert.equal(result.approval_policy,'on-request');assert.equal(result.sandbox,'readOnly');
  result=await ipc.action({action:'update_settings',thread_id:id,thread_settings:{model:'model-b'},daybreak_enabled:true});
  assert.equal(result.model,'model-b');assert.equal(result.daybreak_enabled,true);
  const before=calls.length;
  for (const args of [{thread_settings:{}},{daybreak_enabled:'false'},{thread_settings:[],daybreak_enabled:false},
    {thread_settings:{approvalPolicy:'invented'}},{thread_settings:{developerInstructions:'injected'}},
    {thread_settings:{sandboxPolicy:{type:'readOnly',writableRoots:['secret']}}}]) {
    await assert.rejects(ipc.action({action:'update_settings',thread_id:id,...args}),{code:'invalid_settings'});
  }
  assert.equal(calls.length,before);
  ipc.settingsUpdates.add(id);
  await assert.rejects(ipc.action({action:'update_settings',thread_id:id,daybreak_enabled:false}),{code:'busy'});
  ipc.settingsUpdates.clear();
  let attempts=0;
  for (const reply of [{result:{}},{result:{applied:false}}]) {
    const f=client(), original=f.ipc.request;
    f.ipc.request=async(...args)=>args[0]==='thread-follower-read-model-settings' ? original(...args) : (attempts++,reply);
    await assert.rejects(f.ipc.action({action:'update_settings',thread_id:id,thread_settings:{model:'model-b'}}),
      {code:'settings_not_applied'});
    assert.equal(f.ipc.settingsUpdates.size,0);
  }
  assert.equal(attempts,2);
  const failure=client();let count=0;
  failure.ipc.request=async()=>{count++;throw new DesktopError('outcome_unknown','disconnected');};
  await assert.rejects(failure.ipc.action({action:'update_settings',thread_id:id,daybreak_enabled:false}),
    {code:'outcome_unknown'});
  assert.equal(count,1);assert.equal(failure.ipc.settingsUpdates.size,0);
  const absent=client();absent.ipc.request=async()=>({result:{settings:null}});
  await assert.rejects(absent.ipc.action({action:'settings',thread_id:id}),{code:'settings_unavailable'});
  console.log(JSON.stringify({passed:true,checks:['native_read','same_settings_no_write','daybreak_off_keeps_model',
    'omitted_policy_preserved','model_flag_readback','invalid_settings_rejected','concurrent_update_blocked',
    'missing_ack_rejected','declined_update_rejected','timeout_no_replay','lock_released','missing_settings_rejected'],real_gui_mutations:false}));
})().catch(error=>{console.error(error);process.exitCode=1;});
