'use strict';
const assert = require('assert/strict');
const {decodeMessage} = require('./snapshot-tail');
const {DesktopIPC} = require('./desktop-ipc');
const envelope = state => ({type:'broadcast',method:'thread-stream-state-changed',version:11,
  sourceClientId:'owner',params:{hostId:'local',conversationId:'thread',
    change:{type:'snapshot',conversationState:state}}});
const decode = state => decodeMessage(Buffer.from(JSON.stringify(envelope(state)))).params.change.conversationState;
const turn = (n, status='completed') => ({turnId:'t'+n,status,turnStartedAtMs:n,
  params:{input:[{type:'text',text:'question '+n},{type:'image',url:'data:unused'}]},
  items:[{id:'user-'+n,type:'userMessage',content:[{type:'text',text:'question '+n}]},
    {id:'tool-'+n,type:'commandExecution',output:'unwanted output',command:'unwanted command'},
    {id:'image-'+n,type:'image',image:'data:unused'},
    {id:'answer-'+n,type:'agentMessage',text:'answer '+n,phase:'final_answer'}]});

(async () => {
  const turns = Array.from({length:20},(_,n)=>turn(n));
  let state = decode({id:'thread',title:'title',turns,
    threadRuntimeStatus:{type:'idle',activeFlags:[]},requests:[],
    latestCollaborationMode:{settings:{model:'model',developer_instructions:'MUST NOT LEAK'}}});
  assert.deepEqual(state.turns.map(t=>t.turnId),['t14','t15','t16','t17','t18','t19']);
  assert.equal(state.latestCollaborationMode.settings.model,'model');
  assert(!JSON.stringify(state).includes('MUST NOT LEAK'));
  assert(!JSON.stringify(state).includes('unwanted'));
  assert(!JSON.stringify(state).includes('data:unused'));

  const entities = Object.fromEntries([...turns].reverse().map(t=>[t.turnId,t]));
  state = decode({id:'thread',turnHistory:{kind:'canonical',history:{entitiesByKey:entities,
    islands:[{entries:turns.slice(0,10).map(t=>({value:t.turnId}))},
      {entries:turns.slice(10).map(t=>({value:t.turnId}))}]}}});
  assert.deepEqual(state.turns.map(t=>t.turnId),['t14','t15','t16','t17','t18','t19']);
  turns[0].status='inProgress';
  state = decode({id:'thread',turns});
  assert.equal(state.turns.length,7);
  assert.equal(state.turns[0].turnId,'t0');

  // Large message bodies are bounded, including escaped quotes/backslashes,
  // unicode escape boundaries and multibyte UTF-8 at either slice edge.
  const samples = ['한글🙂"\\\n', 'abc\\\\"\t', '\u0001\u0002\u0003', '\u2028🌏'];
  for (const sample of samples) {
    const t=turn(0), text=sample.repeat(18000);
    t.params.input[0].text=text;
    t.items.at(-1).text=text;
    const result=decode({id:'thread',turns:[t]}).turns[0];
    const user=result.params.input[0].text, answer=result.items.at(-1).text;
    assert(user.length<=2001 && text.startsWith(user));
    assert(answer.length<=12001 && text.endsWith(answer));
  }
  // Exercise explicit JSON \uXXXX escapes, not only JSON.stringify's UTF-8.
  let raw=JSON.stringify(envelope({id:'thread',turns:[{...turn(0),items:[
    {type:'agentMessage',text:'a'.repeat(90000)+'last'}]}]}));
  raw=raw.replace('a'.repeat(90000),'\\u0061'.repeat(90000));
  const answer=decodeMessage(Buffer.from(raw)).params.change.conversationState.turns[0].items[0].text;
  assert.equal(answer,'a'.repeat(11997)+'last');

  const question={id:7,method:'item/tool/requestUserInput',params:{turnId:'t0',
    questions:[{id:'target',header:'Target',question:'Which target?',options:[{label:'staging',description:'test'}]}]}};
  state=decode({id:'thread',turns:[turn(0)],requests:[question,
    {id:8,method:'item/commandExecution/requestApproval',params:{command:'MUST NOT LEAK'}}]});
  assert.equal(state.requests[0].params.questions[0].question,'Which target?');
  assert(!JSON.stringify(state).includes('MUST NOT LEAK'));
  const large=decode({id:'thread',requests:Array.from({length:8},(_,id)=>({...question,id,
    params:{questions:Array.from({length:8},(_,n)=>({id:'q'+n,question:'x'.repeat(10000),
      options:Array.from({length:8},()=>({label:'y'.repeat(2000),description:'z'.repeat(2000)}))}))}}))});
  assert(JSON.stringify(large.requests).length<22000);
  assert(large.requests.every(r=>r.truncated));

  // Approval state survives projection; controls must not include conversation text.
  const ipc = new DesktopIPC();
  ipc.owner = async ()=>'owner';
  ipc.state = async ()=>decode({id:'thread',turns:[turn(0)],requests:[{id:4,method:'approval'}],
    threadRuntimeStatus:{type:'active',activeFlags:['waitingOnApproval']}});
  await assert.rejects(ipc.action({action:'send',thread_id:'thread',prompt:'test'}),{code:'waiting_for_input'});
  ipc.state = async ()=>decode({id:'thread',turns:[turn(0)]});
  const idleStop=await ipc.action({action:'stop',thread_id:'thread'});
  assert(!('answer' in idleStop));assert(!('recent_messages' in idleStop));
  const read=await ipc.action({action:'read',thread_id:'thread'});
  assert(read.recent_messages.length<=8);
  assert(read.recent_messages.reduce((n,m)=>n+m.text.length,0)<=6000);
  assert.equal(read.answer,'answer 0');
  const metadata=decodeMessage(Buffer.from(JSON.stringify(envelope({id:'thread',turns}))),()=>false)
    .params.change.conversationState;
  assert(metadata.turns.length<=2);
  assert(!JSON.stringify(metadata).includes('question '));
  assert(!JSON.stringify(metadata).includes('answer '));
  assert.throws(()=>decodeMessage(Buffer.from('{"type":"broadcast"')));
  assert.deepEqual(decodeMessage(Buffer.from('{"type":"response","result":{"ok":true}}')),
    {type:'response',result:{ok:true}});
  console.log(JSON.stringify({passed:true,checks:['recent_turns','canonical_order','active_turn',
    'no_images_tools_instructions','bounded_strings','unicode_escapes','approval_preserved',
    'controls_without_text','model_tail_budget','question_projection','bounded_questions',
    'malformed_json','ordinary_response'],real_gui_mutations:false}));
})().catch(error=>{console.error(error);process.exitCode=1;});
