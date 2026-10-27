"""Count saved A/B replies with the installed Strata pack's exact tokenizer.

Usage: python measure_tokens.py STRATA_TOOLS TOKENIZER_DIR RESULTS_DIR
Counts tool content, not chat-template overhead or model reasoning.
"""
import hashlib
import json
from pathlib import Path
import sys


def main():
    source, pack, results = map(Path, sys.argv[1:4])
    sys.path.insert(0, str(source))
    from strata_tokenizer import Tokenizer
    vocab = json.loads((pack / 'vocab.json').read_text())
    tokens = [None] * len(vocab)
    for token, index in vocab.items(): tokens[index] = token
    tokenizer = Tokenizer(tokens, (pack / 'merges.txt').read_text().splitlines(),
                          json.loads((pack / 'token_type.json').read_text()))
    rows = []
    for file in ['replies.json', 'public-replies.json']:
        for row in json.loads((results / file).read_text()):
            a, b = [len(tokenizer.encode(row[k])) for k in ['before', 'after']]
            rows.append({'case': row['case'], 'before_tokens': a, 'after_tokens': b,
                         'reduction_percent': round((1 - b / a) * 100, 2)})
    report = {'tokenizer': 'installed Strata pack, qwen35 byte BPE; parse_special=False',
              'sha256': {n: hashlib.sha256((pack / n).read_bytes()).hexdigest()
                         for n in ['vocab.json', 'merges.txt', 'token_type.json']},
              'cases': rows}
    (results / 'token-comparison.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == '__main__': main()
