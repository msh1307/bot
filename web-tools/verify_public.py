"""Compare both versions on the same public responses, without an LLM.

Uses real network/renderer once per URL, then replays that source in memory.
Timing is capture timing, not an A/B network-speed benchmark.
"""
import importlib.util
import json
from pathlib import Path
import sys
import time


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    old, new = [load(p, n) for p, n in zip(sys.argv[1:3], ['old_web', 'new_web'])]
    out = Path(sys.argv[3]); out.mkdir(parents=True, exist_ok=True)
    replies, cases = [], []
    capture = {}

    def shared(kind, original):
        def call(*a, **kw):
            key = (kind, repr(a), repr(kw))
            if key not in capture:
                started = time.monotonic()
                try:
                    capture[key] = (original(*a, **kw), None)
                except Exception as e:
                    capture[key] = (None, e)
                cases.append({'capture': kind, 'seconds': round(time.monotonic() - started, 3),
                              'ok': capture[key][1] is None})
            value, error = capture[key]
            if error: raise error
            return value
        return call

    for kind in ['fetch_public', 'render_public', 'local_search']:
        fn = shared(kind, getattr(old, kind))
        setattr(old, kind, fn); setattr(new, kind, fn)

    corpus = [
        ('python_en', 'https://docs.python.org/3/library/asyncio-task.html', 'TaskGroup'),
        ('python_ko', 'https://docs.python.org/ko/3/library/asyncio-task.html', 'TaskGroup'),
        ('kernel_text', 'https://raw.githubusercontent.com/torvalds/linux/master/Documentation/RCU/whatisRCU.rst', 'rcu_read_lock'),
        ('pwn2own_js', 'https://pwn2own.zerodayinitiative.com/', 'Rank'),
    ]
    for name, url, needle in corpus:
        args = {'url': url, 'backend': 'local'}
        a, b = old.read_page(args), new.read_page(args)
        replies.append({'case': name + '_default', 'before': a, 'after': b})
        da, db = json.loads(a), json.loads(b)
        row = {'case': name, 'old_success': da.get('success'), 'new_success': db.get('success')}
        if db.get('success'):
            follow = dict(args, ref=db['ref']); follow.pop('url')
            found = json.loads(new.read_page(dict(follow, action='find', query=needle, version=db['version'])))
            row['find_matches'] = len(found.get('matches', []))
            row['source_capped'] = db.get('source_truncated', False)
            row['next_offset'] = db.get('next_offset')
            if db.get('next_offset'):
                more = json.loads(new.read_page(dict(follow, offset=db['next_offset'], version=db['version'])))
                row['continued'] = more.get('success') and more.get('offset') == db['next_offset']
            # Both old and new must retain the original, not an AI paraphrase.
            old_pages = [p for _, p in old._pages.values() if p.get('url') == db['url'] and '_text' in p]
            new_pages = [p for _, p in new._pages.values() if p.get('url') == db['url'] and '_text' in p]
            if old_pages and new_pages:
                row['same_extracted_source'] = old_pages[-1]['_text'] == new_pages[-1]['_text']
                row['source_chars_old'] = len(old_pages[-1]['_text'])
                row['source_chars_new'] = len(new_pages[-1]['_text'])
            aa = old.read_page(dict(args, char_limit=18000))
            bb = new.read_page(dict(args, char_limit=18000))
            replies.append({'case': name + '_large_request', 'before': aa, 'after': bb})
        else:
            row['error'] = db.get('error')
        cases.append(row)

    for name, query in [
        ('search_english', 'site:docs.python.org TaskGroup exception handling'),
        ('search_pdf', 'site:kernel.org filetype:pdf RCU'),
        ('search_korean', '파이썬 asyncio TaskGroup 예외 처리'),
    ]:
        args = {'query': query, 'engine': 'local'}
        if name == 'search_korean': args['language'] = 'ko'
        a, b = old.search(args), new.search(args)
        replies.append({'case': name, 'before': a, 'after': b})
        d = json.loads(b)
        cases.append({'case': name, 'success': d.get('success'), 'returned': d.get('returned'),
                      'next': d.get('next'), 'backend': d.get('backend'), 'error': d.get('error')})

    (out / 'public-replies.json').write_text(json.dumps(replies, ensure_ascii=False))
    (out / 'public-verification.json').write_text(json.dumps(cases, ensure_ascii=False, indent=2))
    print(json.dumps(cases, ensure_ascii=False), flush=True)


if __name__ == '__main__': main()
