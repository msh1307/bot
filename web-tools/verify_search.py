"""Frozen-source regressions for output budgets, navigation and extraction.

Run with the deployed web worker's Python environment. No LLM calls or writes
outside the requested report directory. Fetching is replaced only in fixture cases.
"""
import importlib.util
import json
from pathlib import Path
import re
import sys


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    baseline, candidate, out = map(Path, sys.argv[1:4])
    old, new = load(baseline, 'baseline_web'), load(candidate, 'candidate_web')
    out.mkdir(parents=True, exist_ok=True)
    cases = []
    replies = []

    def record(name, before, after):
        cases.append({'case': name, 'before_chars': len(before), 'after_chars': len(after),
                      'before_bytes': len(before.encode()), 'after_bytes': len(after.encode())})
        replies.append({'case': name, 'before': before, 'after': after})

    # Body includes long lines, empty lines, code, Korean and a final marker.
    lines = ['# Evidence', '', '```c', 'if (count > limit) return -EINVAL;', '```']
    lines += [f'Row {i:04d}: ' + ('소유권과 경계 조건. ' if i % 2 else 'Ownership and boundary conditions. ') * (70 if i % 5 == 0 else 3) for i in range(220)]
    lines += ['', 'FINAL_NEEDLE_5824 is a literal source fact.']
    source = '\n'.join(lines)
    links = [{'title': f'Link {i}', 'url': f'https://example.com/doc/{i}?value=' + 'x' * 1200} for i in range(30)]
    page = {'url': 'https://example.com/article', '_text': source, '_links': links,
            'source_dates': ['2026-10-26'], 'text_available': True}
    for limit in [2400, 6000, 18000]:
        before = old.page_payload(page, limit)
        after = new.page_payload(page, min(limit, 6000))
        assert len(after.encode()) <= min(10000, max(4096, min(limit, 6000) * 2))
        record(f'page_limit_{limit}', before, after)

    # Reconstruct exact source intervals from every continuation, including splits
    # inside a long line. A smaller reply must not skip or duplicate source bytes.
    offset, pages = 0, 0
    while offset < len(source):
        d = json.loads(new.page_payload(page, 2400, offset=offset))
        end = d.get('next_offset', len(source))
        assert end > offset
        raw = re.sub(r'(?m)^L\d+: ', '', d['content'])
        assert raw == source[offset:end].rstrip('\n')
        assert d['version'] == new.page_version(page)
        offset, pages = end, pages + 1
    found = json.loads(new.page_payload(page, 2400, action='find', query='FINAL_NEEDLE_5824'))
    assert any('FINAL_NEEDLE_5824' in m['content'] for m in found['matches'])
    assert 'next_offset' not in found
    missing = json.loads(new.page_payload(page, 2400, action='find', query='NOT_PRESENT_7129'))
    assert missing['matches'] == [] and missing['warning']

    all_links, offset = [], 0
    while True:
        d = json.loads(new.page_payload(page, 2400, links_limit=60, action='links', offset=offset))
        assert len(json.dumps(d, ensure_ascii=False, separators=(',', ':')).encode()) <= 4800
        all_links.extend(x['url'] for x in d['links'])
        if 'next_links_offset' not in d: break
        assert d['next_links_offset'] > offset
        offset = d['next_links_offset']
    assert all_links == [x['url'] for x in links]

    # Search keeps ordering and continuation even when long URLs exhaust the cap.
    rows = [{'url': f'https://example.com/result/{i}?q=' + 'x' * 1400,
             'title': 'Title ' + str(i), 'description': 'snippet ' * 80,
             'published_date_hint': '2026-10-26', 'engines': ['one', 'two']} for i in range(20)]
    for mod in (old, new):
        mod.local_search = lambda *a, **k: {'success': True, 'data': {'web': rows}, 'backend': 'fixture', 'single_page': True}
        mod.link_shape = lambda u: u.startswith('https://example.com/')
    args = {'query': 'site:example.com "exact term" filetype:pdf', 'engine': 'local', 'limit': 10}
    before, after = old.search(args), new.search(args)
    record('search_long_urls', before, after)
    observed, offset = [], 0
    while True:
        d = json.loads(new.search(dict(args, offset=offset)))
        assert d['success'] and len(new.encode_result(d).encode()) <= 6000
        observed.extend(x['url'] for x in d['data']['web'])
        if not d['next']: break
        assert d['next']['page'] == 1 and d['next']['offset'] > offset
        offset = d['next']['offset']
    assert observed == [x['url'] for x in rows]

    # Default rendering is preserved: static navigation alone must not hide JS data.
    article = '<html><article><h1>Release notes</h1>' + ''.join(f'<p>Paragraph {i}: A deterministic result with source evidence and documented constraints.</p>' for i in range(40)) + '</article></html>'
    counts = {'fetch': 0, 'render': 0}
    raw_html = article
    def fetch(url):
        counts['fetch'] += 1
        return url, 'text/html', raw_html
    def render(url):
        counts['render'] += 1
        return url, article.replace('Release notes', 'Rendered release notes')
    new.fetch_public, new.render_public, new.validate_url = fetch, render, lambda u: None
    new.clear_cache(clear_refs=True)
    first = json.loads(new.read_page({'url': page['url']}))
    assert first['success'] and counts == {'fetch': 1, 'render': 1}
    assert 'links' not in first
    next_args = {'ref': first['ref'], 'offset': first['next_offset'], 'version': first['version']}
    second = json.loads(new.read_page(next_args))
    assert second['success'] and counts == {'fetch': 1, 'render': 1}
    new.clear_cache()
    expired = json.loads(new.read_page(next_args))
    assert not expired['success'] and 'expired' in expired['error'] and counts['fetch'] == 1
    raw_html = '<html><div id="root"></div><script src="app.js"></script></html>'
    js = json.loads(new.read_page({'url': 'https://example.com/app'}))
    assert js['success'] and counts['render'] == 2
    always = json.loads(new.read_page({'url': page['url'], 'render': 'always'}))
    assert always['success'] and counts['render'] == 3
    static = json.loads(new.read_page({'url': page['url'], 'render': 'never'}))
    assert static['success'] and counts['render'] == 3
    # Real model regression: it reads mode=full, then follows its ref/version
    # without repeating mode/backend. The version must select the same snapshot.
    raw_html = article
    full = json.loads(new.read_page({'url': page['url'], 'mode': 'full', 'backend': 'local'}))
    before_counts = dict(counts)
    pinned = json.loads(new.read_page({'ref': full['ref'], 'version': full['version'],
                                      'action': 'find', 'query': 'Paragraph 25'}))
    assert pinned['success'] and pinned['version'] == full['version'] and pinned['matches']
    assert counts == before_counts
    implicit_find = json.loads(new.read_page({'ref': full['ref'], 'version': full['version'],
                                             'query': 'Paragraph 25'}))
    assert implicit_find['matches'] and 'content' not in implicit_find
    invalid_query = json.loads(new.read_page({'ref': full['ref'], 'query': 'Paragraph 25', 'action': 'read'}))
    assert not invalid_query['success']  # Never silently ignore a requested search.
    wrong_url = json.loads(new.read_page({'url': 'https://example.com/other', 'version': full['version']}))
    assert not wrong_url['success']
    refresh = json.loads(new.read_page({'ref': full['ref'], 'version': full['version'], 'refresh': True}))
    assert not refresh['success']

    # Invalid public URLs are still denied by unmodified production validation.
    safety = load(candidate, 'safety_web')
    rejected = []
    for url in ['file:///etc/passwd', 'http://127.0.0.1/', 'http://169.254.169.254/', 'https://u:p@example.com/', 'https://example.com:1234/']:
        try: safety.validate_url(url)
        except ValueError: rejected.append(url)
    assert len(rejected) == 5

    class Registry:
        def __init__(self): self.defs = []
        def register_tool(self, name, toolset, definition, handler): self.defs.append(definition)
    a, b = Registry(), Registry()
    old.register(a); new.register(b)
    record('tool_schemas', json.dumps(a.defs, ensure_ascii=False), json.dumps(b.defs, ensure_ascii=False))
    result = {'passed': True, 'cases': cases, 'reassembled_pages': pages,
              'search_results_preserved': len(observed), 'links_preserved': len(all_links),
              'source_versions_enforced': True, 'snapshot_options_optional': True,
              'query_implies_find': True,
              'static_and_rendered_paths': True,
              'public_url_rejections': len(rejected), 'cache_storage': 'process RAM'}
    (out / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    (out / 'replies.json').write_text(json.dumps(replies, ensure_ascii=False))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__': main()
