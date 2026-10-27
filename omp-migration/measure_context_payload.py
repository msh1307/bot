"""Count synthetic OMP payloads with the deployed Strata template and tokenizer.

Usage: python measure_context_payload.py STRATA_SOURCE TOKENIZER_DIR REPORT_DIR
No inference, API call or transcript modification.
"""
import hashlib
import json
from pathlib import Path
import sys

source, pack, report = map(Path, sys.argv[1:4])
sys.path[:0] = [str(source), str(source / 'tools')]
from serve.frontend import ChatTemplate, openai_to_messages, mark_think_literals, unmark_think_literals
from strata_tokenizer import Tokenizer

vocab = json.loads((pack / 'vocab.json').read_text())
tokens = [None] * len(vocab)
for token, index in vocab.items(): tokens[index] = token
config = json.loads((pack / 'tokenizer.json').read_text())
tok = Tokenizer(tokens, (pack / 'merges.txt').read_text().split('\n'),
                json.loads((pack / 'token_type.json').read_text()),
                config.get('pre') or 'qwen35', config.get('special_ids') or None)
template = ChatTemplate(pack / 'chat_template.jinja')
data = json.loads((report / 'synthetic-payloads.json').read_text())
results = {}
for variant, requests in data.items():
    rows = []
    for request in requests:
        messages, tools, kwargs = openai_to_messages(request)
        marked, marked_tools, _ = mark_think_literals(messages, tools)
        prompt, spans = unmark_think_literals(template.render(marked, marked_tools, **kwargs))
        rows.append({'input_tokens': len(tok.encode(prompt, True, spans)),
                     'reasoning_tokens': sum(len(tok.encode(m.get('reasoning_content', '')))
                                             for m in messages),
                     'tool_result_tokens': sum(len(tok.encode(str(m.get('content', ''))))
                                               for m in messages if m['role'] == 'tool')})
    results[variant] = rows
result = {'scope': 'Exact deployed template/tokenizer; synthetic scripted provider requests',
    'model_inference': False, 'request_tokens': results,
    'source_sha256': {name: hashlib.sha256((pack / name).read_bytes()).hexdigest()
                      for name in ('vocab.json', 'merges.txt', 'token_type.json', 'chat_template.jinja')}}
(report / 'context-tokens.json').write_text(json.dumps(result, indent=2))
print(json.dumps(result), flush=True)
