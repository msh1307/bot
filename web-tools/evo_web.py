"""Public read-only web tools. No account cookies, arbitrary headers or shell access."""
import json
from collections import OrderedDict
import html as html_text
import ipaddress
import re
import threading
import time
import socket
from urllib.parse import urlsplit, urljoin, urldefrag
import uuid
import hashlib

MAX_BYTES = 2 * 1024 * 1024
MAX_TEXT = 200000
SEARCH_ENDPOINT = 'http://127.0.0.1:18082/search'
FREE_PROVIDERS = ('parallel', 'exa', 'keenable', 'firecrawl')
SEARCH_ENGINES = ('auto', 'local', 'google', 'bing', 'duckduckgo', 'yahoo') + FREE_PROVIDERS
_free_lock = threading.Lock()
_free_cooldowns = {}
USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36')
_pages = OrderedDict()
_pages_lock = threading.Lock()
MAX_LINKS = 60
MAX_REFS = 512
REF_TTL = 1800
_refs = OrderedDict()
_ref_urls = {}
_refs_lock = threading.Lock()
_ref_prefix = 'r' + uuid.uuid4().hex[:4] + '-'
_ref_serial = 0
_render_lock = threading.Lock()
RENDER_SOCKET = '/run/evo-web-render.sock'


def render_public(url):
    """A local socket-activated worker renders one page, then exits."""
    validate_url(url)
    with _render_lock, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(27)
        connection.connect(RENDER_SOCKET)
        connection.sendall((json.dumps({'url': url}) + '\n').encode())
        connection.shutdown(socket.SHUT_WR)
        body = bytearray()
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            if len(body) + len(chunk) > MAX_BYTES:
                raise ValueError('Rendered response exceeded 2 MiB.')
            body.extend(chunk)
    if not body:
        raise ValueError('Page renderer closed without a result.')
    data = json.loads(body)
    if not data.get('success'):
        raise ValueError(data.get('error') or 'Page rendering failed.')
    rendered_url = data.get('url') or url
    validate_url(rendered_url)
    return rendered_url, data['html']


def encode_result(result):
    return json.dumps(result, ensure_ascii=False, separators=(',', ':'))


def remember_url(url):
    """Short-lived navigation references, never credentials or authorization."""
    global _ref_serial
    now = time.monotonic()
    with _refs_lock:
        ref = _ref_urls.get(url)
        if ref in _refs and _refs[ref][0] > now:
            _refs.move_to_end(ref)
            return ref
        if ref:
            _refs.pop(ref, None)
        _ref_serial += 1
        ref = _ref_prefix + format(_ref_serial, 'x')
        _refs[ref] = (now + REF_TTL, url)
        _ref_urls[url] = ref
        while len(_refs) > MAX_REFS:
            old_ref, (_, old_url) = _refs.popitem(last=False)
            if _ref_urls.get(old_url) == old_ref:
                del _ref_urls[old_url]
        return ref


def resolve_ref(ref):
    with _refs_lock:
        entry = _refs.get(ref)
        if not entry or entry[0] <= time.monotonic():
            if entry:
                del _refs[ref]
                if _ref_urls.get(entry[1]) == ref:
                    del _ref_urls[entry[1]]
            raise ValueError('Unknown or expired ref. Search or read its source page again; do not guess a URL.')
        _refs.move_to_end(ref)
        return entry[1]


def link_shape(url):
    """Cheap candidate filtering; fetch_public still validates DNS before reading."""
    from agent.redact import _PREFIX_RE
    from tools.url_safety import sensitive_query_param_name
    from urllib.parse import unquote
    if len(url) > 2048:
        return False
    try:
        p = urlsplit(url)
        if p.scheme not in ('http', 'https') or not p.hostname:
            return False
        if p.username or p.password or p.port not in (None, 80, 443):
            return False
        if sensitive_query_param_name(url) or _PREFIX_RE.search(unquote(url)):
            return False
        host = p.hostname.lower().rstrip('.')
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return '.' in host
    except ValueError:
        return False


def extract_links(raw, source_url):
    """Read real HTML links using lxml and w3lib's URL escaping, without a crawler."""
    from lxml import html
    from w3lib.url import safe_url_string
    tree = html.fromstring(raw, base_url=source_url)
    base = source_url
    bases = tree.xpath('//base[@href]/@href')
    if bases:
        try:
            base = urljoin(source_url, bases[0])
            urlsplit(base)
        except ValueError:
            base = source_url
    nodes = tree.xpath('//a[@href] | //area[@href] | //link[@href and @rel]')
    scan_truncated = len(nodes) > 6000
    for node in nodes[6000:]:
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)
    in_content = 'ancestor::article or ancestor::main or ancestor::*[@role="main"]'
    in_nav = 'ancestor::nav or ancestor::header or ancestor::footer or ancestor::*[@role="navigation"]'
    anchor = '//*[self::a or self::area][@href]'
    groups = [
        '//*[self::a or self::area or self::link][@href and contains(concat(" ", normalize-space(@rel), " "), " next ")]',
        '//*[self::a or self::area or self::link][@href and contains(concat(" ", normalize-space(@rel), " "), " prev ")]',
        anchor + '[' + in_content + '][not(' + in_nav + ')]',
        anchor + '[not(' + in_content + ')][not(' + in_nav + ')]',
        anchor + '[' + in_nav + ']',
    ]
    binary_suffixes = {'pdf', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'ico',
                       'mp3', 'mp4', 'webm', 'wav', 'zip', 'gz', 'tar', '7z',
                       'exe', 'dmg', 'iso', 'woff', 'woff2', 'ttf'}
    seen = set()
    links = []
    source = urldefrag(source_url)[0]
    for xpath in groups:
        for node in tree.xpath(xpath):
            try:
                target = urldefrag(safe_url_string(urljoin(base, node.get('href', ''))))[0]
                if not link_shape(target):
                    continue
                if urlsplit(target).path.lower().rsplit('.', 1)[-1] in binary_suffixes:
                    continue
            except (ValueError, UnicodeError):
                continue
            if target == source or target in seen:
                continue
            seen.add(target)
            if len(links) == MAX_LINKS:
                return links, True
            label = ' '.join(node.text_content().split()) or target
            links.append({'title': label[:240], 'url': target, 'ref': remember_url(target)})
    return links, scan_truncated


def cached_page(url):
    with _pages_lock:
        entry = _pages.get(url)
        if not entry:
            return None
        expires, page = entry
        if expires <= time.monotonic():
            del _pages[url]
            return None
        _pages.move_to_end(url)
        return dict(page)


def clear_cache(clear_refs=False, **kwargs):
    with _pages_lock:
        _pages.clear()
    if clear_refs:
        with _refs_lock:
            _refs.clear()
            _ref_urls.clear()


def finish_search_turn(**kwargs):
    clear_cache()


def cache_page(url, page):
    with _pages_lock:
        _pages[url] = (time.monotonic() + REF_TTL, dict(page))
        _pages.move_to_end(url)
        while len(_pages) > 16:
            _pages.popitem(last=False)


def cached_snapshot(url, version):
    """A version pins extracted content, even if callers omit its fetch options."""
    address = urldefrag(url)[0]
    now = time.monotonic()
    with _pages_lock:
        for key, (expires, page) in reversed(_pages.items()):
            if expires <= now or '_text' not in page:
                continue
            requested = key[-1] if isinstance(key, tuple) and key[0] == 'read' else ''
            if address not in (urldefrag(requested)[0], urldefrag(page['url'])[0]):
                continue
            if page_version(page) == version:
                _pages.move_to_end(key)
                return dict(page)
    return None


def clean_snippet(value):
    return html_text.unescape(re.sub(r'<[^>]+>', '', str(value or '')))[:1200]


class FreeProviderError(ValueError):
    def __init__(self, message, cooldown=60):
        super().__init__(message)
        self.cooldown = cooldown


def free_request(provider, payload, deadline, *, fetch=False):
    """Anonymous endpoints only; bounded transport, no environment credentials."""
    import httpx
    from plugins.web import keyless_mcp as native
    urls = {'parallel': native.PARALLEL_MCP_URL, 'exa': native.EXA_MCP_URL,
            'keenable': native.KEENABLE_API_URL + ('/v1/fetch/public' if fetch else '/v1/search/public'),
            'firecrawl': 'https://api.firecrawl.dev/v2/' + ('scrape' if fetch else 'search')}
    headers = {'User-Agent': 'hermes-evo-web', 'Accept': 'application/json, text/event-stream'}
    if provider == 'keenable':
        headers['X-Keenable-Title'] = 'hermes-agent'
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FreeProviderError('time budget exhausted')
    timeout = httpx.Timeout(min(7, remaining), connect=min(3, remaining))
    method = 'GET' if provider == 'keenable' and fetch else 'POST'
    request = {'params': payload} if method == 'GET' else {'json': payload}
    with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False) as client:
        with client.stream(method, urls[provider], headers=headers, **request) as response:
            if response.status_code >= 300:
                retry = response.headers.get('retry-after', '')
                cooldown = min(3600, max(60, int(retry))) if retry.isdecimal() else 300
                raise FreeProviderError('HTTP ' + str(response.status_code), cooldown)
            body = bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise FreeProviderError('time budget exhausted')
                if len(body) + len(chunk) > MAX_BYTES:
                    raise FreeProviderError('response exceeded 2 MiB')
                body.extend(chunk)
    text = body.decode('utf-8', errors='replace')
    if provider in ('parallel', 'exa'):
        return native._parse_mcp_body(text)
    data = json.loads(text)
    if data.get('success') is False or data.get('error'):
        raise FreeProviderError('provider returned an error', 300)
    return data


def free_call(provider, query, limit, deadline, objective='', queries=None, url=''):
    """Reuse Hermes' vendor schemas and parsers behind our compact tool contract."""
    from plugins.web import keyless_mcp as native
    fetch = bool(url)
    if provider == 'parallel':
        arguments = {'session_id': native._SESSION_ID}
        if fetch:
            arguments.update(urls=[url], full_content=True)
        else:
            arguments.update(objective=objective or query, search_queries=queries or [query])
        payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'web_fetch' if fetch else 'web_search', 'arguments': arguments}}
    elif provider == 'exa':
        payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'web_fetch_exa' if fetch else 'web_search_exa',
            'arguments': {'urls': [url]} if fetch else {'query': query, 'numResults': min(limit, 20)}}}
    elif provider == 'keenable':
        payload = {'url': url} if fetch else {'query': query, 'max_results': min(limit, 20)}
    else:
        payload = {'url': url, 'formats': ['markdown']} if fetch else {'query': query, 'limit': min(limit, 20)}
    data = free_request(provider, payload, deadline, fetch=fetch)
    if provider == 'parallel':
        rows = json.loads(data).get('results') or []
        if fetch:
            # A provider's redirected URL is checked by the caller before caching.
            return next(({'url': row.get('url') or url,
                          'text': row.get('full_content') or row.get('content') or '\n\n'.join(row.get('excerpts') or [])}
                         for row in rows if isinstance(row, dict)), {})
        return [{'url': row.get('url') or '', 'title': row.get('title') or '',
                 'description': ' '.join(row.get('excerpts') or []),
                 'published_date_hint': row.get('publish_date')}
                for row in rows[:20] if isinstance(row, dict)]
    if provider == 'exa':
        return {'url': url, 'text': data} if fetch else native._parse_exa_search_text(data, limit)
    if provider == 'keenable':
        if fetch:
            return {'url': data.get('url') or url, 'text': data.get('content') or ''}
        return [{'url': row.get('url') or '', 'title': row.get('title') or '',
                 'description': row.get('snippet') or row.get('description') or ''}
                for row in (data.get('results') or [])[:20] if isinstance(row, dict)]
    from plugins.web.firecrawl.provider import _extract_web_search_results, _extract_scrape_payload
    if fetch:
        return {'url': url, 'text': _extract_scrape_payload(data).get('markdown') or ''}
    return _extract_web_search_results(data)


def free_search(query, limit=20, provider='auto', objective='', queries=None):
    deadline = time.monotonic() + 28
    warnings = []
    order = FREE_PROVIDERS if provider == 'auto' else (provider,)
    for name in order:
        with _free_lock:
            cooling = _free_cooldowns.get(name, 0) > time.monotonic()
        if cooling:
            warnings.append(name + ': temporarily unavailable')
            continue
        if deadline - time.monotonic() < 1:
            break
        try:
            rows = free_call(name, query, limit, deadline, objective, queries)
            hits, seen = [], set()
            for row in rows:
                url = str(row.get('url') or '')
                if not link_shape(url) or url in seen:
                    continue
                seen.add(url)
                hits.append({'url': url, 'title': str(row.get('title') or '')[:400],
                             'description': clean_snippet(row.get('description')),
                             'published_date_hint': row.get('published_date_hint')})
                if len(hits) >= limit:
                    break
            if hits:
                return {'success': True, 'data': {'web': hits}, 'backend': name + '_free',
                        'warnings': warnings, 'single_page': True}
            warnings.append(name + ': no results')
        except Exception as error:
            with _free_lock:
                _free_cooldowns[name] = time.monotonic() + getattr(error, 'cooldown', 60)
            warnings.append(name + ': ' + (str(error)[:100] if isinstance(error, FreeProviderError) else type(error).__name__))
    return {'success': False, 'error': 'Free search unavailable: ' + '; '.join(warnings), 'warnings': warnings}


def free_extract(url, provider='auto'):
    """Optional recovery for unreadable public pages; never uploads local HTML."""
    validate_url(url)
    deadline = time.monotonic() + 20
    order = FREE_PROVIDERS if provider == 'auto' else (provider,)
    for name in order:
        with _free_lock:
            if _free_cooldowns.get(name, 0) > time.monotonic():
                continue
        if deadline - time.monotonic() < 1:
            break
        try:
            result = free_call(name, '', 1, deadline, url=url)
            text = str(result.get('text') or '')
            target = str(result.get('url') or url)
            validate_url(target)
            if not text.strip():
                continue
            # Only links actually present in the returned Markdown are navigable.
            links, seen = [], set()
            for title, destination in re.findall(r'\[([^\]\n]{0,200})\]\(([^\s)]+)\)', text[:MAX_TEXT]):
                destination = urljoin(target, destination)
                if link_shape(destination) and destination not in seen:
                    seen.add(destination)
                    links.append({'title': title[:200], 'url': destination})
            return {'url': target, '_text': text[:MAX_TEXT], 'text_available': True,
                    '_links': links[:MAX_LINKS], '_links_truncated': len(links) > MAX_LINKS,
                    'stored_truncated': len(text) > MAX_TEXT, 'provider': name + '_free'}
        except Exception as error:
            with _free_lock:
                _free_cooldowns[name] = time.monotonic() + getattr(error, 'cooldown', 60)
    raise ValueError('No readable content from free providers.')


def local_search(query, limit, time_range, language, engine='auto', page=1):
    """Fixed loopback endpoint: queries go to our own SearXNG instance."""
    import httpx
    source_engine = 'google cse' if engine == 'google' else engine
    with httpx.Client(timeout=httpx.Timeout(14, connect=2), trust_env=False,
                      follow_redirects=False) as client:
        params = {'q': query, 'format': 'json',
            'language': language, 'time_range': time_range,
            'safesearch': 0, 'pageno': page}
        if engine == 'auto':
            params['categories'] = 'general'
        else:
            params['engines'] = source_engine
        response = client.get(SEARCH_ENDPOINT, params=params)
        response.raise_for_status()
        if len(response.content) > MAX_BYTES:
            raise ValueError('Search response exceeded 2 MiB.')
        data = response.json()
    hits, seen = [], set()
    for hit in data.get('results', []):
        url = str(hit.get('url') or '')
        if not link_shape(url) or url in seen:
            continue
        sources = hit.get('engines') or [hit.get('engine')]
        if not isinstance(sources, list):
            sources = [sources]
        if engine != 'auto' and source_engine not in sources:
            continue
        seen.add(url)
        hits.append({'title': str(hit.get('title') or '')[:400], 'url': url,
            'description': clean_snippet(hit.get('content')),
            'published_date_hint': hit.get('publishedDate') or hit.get('pubdate'),
            'engines': [str(name)[:40] for name in sources[:10] if name]})
        if len(hits) >= limit:
            break
    warnings = [str(item)[:160] for item in data.get('unresponsive_engines', [])[:10]]
    if not hits and warnings:
        raise ValueError('Search engines returned no results: ' + '; '.join(warnings))
    return {'success': True, 'data': {'web': hits}, 'backend': 'searxng', 'warnings': warnings}


def search(args, **kwargs):
    try:
        query = str(args.get('query', '')).strip()
        if not query or len(query) > 1000:
            raise ValueError('Provide a search query of 1–1000 characters.')
        limit = max(1, min(10, int(args.get('limit', 5))))
        engine = str(args.get('engine', 'auto')).lower()
        page = int(args.get('page', 1))
        offset = int(args.get('offset', 0))
        snippet_chars = int(args.get('snippet_chars', 200))
        if engine not in SEARCH_ENGINES:
            raise ValueError('Invalid search engine.')
        if not 1 <= page <= 20 or not 0 <= offset <= 50:
            raise ValueError('page must be 1–20 and offset 0–50.')
        if engine == 'bing' and page != 1:
            raise ValueError('The installed Bing adapter supports one search page; use its returned offsets or another engine.')
        if engine == 'google' and page > 5:
            raise ValueError('Google CSE supports up to five search pages; use its returned offsets or refine the query.')
        if not 80 <= snippet_chars <= 1200:
            raise ValueError('snippet_chars must be 80–1200; use web_read for the full page.')
        time_range = str(args.get('time_range', ''))
        language = str(args.get('language', 'en'))
        if time_range not in ('', 'day', 'week', 'month', 'year'):
            raise ValueError('Invalid time_range.')
        if language not in ('en', 'ko', 'all'):
            raise ValueError('language must be en, ko, or all.')
        objective = str(args.get('objective', '')).strip()
        queries = args.get('search_queries', [query])
        if len(objective) > 1000 or not isinstance(queries, list) or not 1 <= len(queries) <= 3 or any(not isinstance(q, str) or not q.strip() or len(q) > 1000 for q in queries):
            raise ValueError('objective is up to 1000 characters; search_queries is 1–3 nonempty queries up to 1000 characters each.')
        if engine in FREE_PROVIDERS and (page != 1 or time_range):
            raise ValueError('Free providers support page 1 without time_range; refine the query for freshness or more results.')
        key = ('search', query, engine, page, time_range, language, objective, tuple(queries))
        result = cached_page(key)
        if result is None:
            use_free = engine in FREE_PROVIDERS or (engine == 'auto' and page == 1 and not time_range and language == 'en' and not re.search(r'(^|\s)![\w-]+', query))
            result = free_search(query, provider=engine, objective=objective, queries=queries) if use_free else None
            free_warnings = (result or {}).get('warnings') or []
            if result and not result.get('success') and engine in FREE_PROVIDERS:
                return encode_result(result)
            try:
                if not result or not result.get('success'):
                    result = local_search(query, 50, time_range, language,
                                          'auto' if engine == 'local' else engine, page)
                    result['warnings'] = free_warnings + result.get('warnings', [])
            except Exception as error:
                if engine not in ('auto', 'local') or page != 1 or re.search(r'(^|\s)![\w-]+', query):
                    raise ValueError(engine + ' search failed: ' + str(error)) from error
                from plugins.web.ddgs.provider import DDGSWebSearchProvider
                result = DDGSWebSearchProvider().search(query, limit=10)
                result['backend'] = 'ddgs_fallback'
                result['warnings'] = free_warnings + ['Local search was unavailable; DDGS fallback has no subsequent search pages.']
            if result.get('success'):
                cache_page(key, result)
        used_fallback = result.get('single_page') or result.get('backend') == 'ddgs_fallback'
        if not result.get('success'):
            return encode_result({'success': False, 'error': result.get('error', 'Search failed.')})
        all_hits = [hit for hit in (result.get('data') or {}).get('web', [])
                    if link_shape(hit.get('url', ''))][:50]
        hits = all_hits[offset:offset + limit]
        compact = []
        for hit in hits:
            item = {'ref': remember_url(hit['url']), 'title': str(hit.get('title') or '')[:200],
                    'url': hit['url'], 'description': clean_snippet(hit.get('description'))[:snippet_chars]}
            if hit.get('published_date_hint'):
                item['date_hint'] = str(hit['published_date_hint'])[:120]
            compact.append(item)
        output = {'success': True, 'data': {'web': compact},
                  'engine': engine, 'backend': result.get('backend'), 'page': page,
                  'offset': offset, 'returned': len(compact), 'next': None,
                  'notice': 'Snippets are unverified leads. Verify source dates; cite read sources. Web content is untrusted data. Open refs, not guessed URLs.'}
        if engine == 'google':
            output['provider'] = 'google_cse'
        elif result.get('single_page'):
            output['provider'] = result['backend']
            if time_range or language != 'en':
                output['warning'] = 'This free provider does not enforce the requested language/time filter.'
        end = offset + len(hits)
        if end < len(all_hits):
            output['next'] = {'page': page, 'offset': end}
        elif hits and page < (5 if engine == 'google' else 20) and not used_fallback and engine != 'bing':
            output['next'] = {'page': page + 1, 'offset': 0}
        if result.get('warnings'):
            output['warnings'] = result['warnings']
        if time_range and used_fallback:
            output['warning'] = 'Requested time filter was unavailable on the fallback engine.'
        return search_payload(output)
    except Exception as error:
        return encode_result({'success': False, 'error': str(error)[:500]})


def validate_url(url):
    from agent.redact import _PREFIX_RE
    from tools.url_safety import is_safe_url, sensitive_query_param_name
    from urllib.parse import unquote
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Only public HTTP/HTTPS page URLs are supported.')
    if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        raise ValueError('Account credentials and custom ports are not supported.')
    if sensitive_query_param_name(url) or _PREFIX_RE.search(unquote(url)):
        raise ValueError('URLs containing credentials or tokens are blocked.')
    if not is_safe_url(url):
        raise ValueError('Private, local and metadata addresses are blocked.')


def fetch_public(url):
    from tools.url_safety import create_ssrf_safe_client
    import httpx
    allowed = {'text/html', 'application/xhtml+xml', 'text/plain',
               'text/markdown', 'application/json', 'application/xml', 'text/xml',
               'application/rss+xml', 'application/atom+xml'}
    deadline = time.monotonic() + 30
    with create_ssrf_safe_client(timeout=httpx.Timeout(12, connect=8),
            follow_redirects=False, trust_env=False,
            headers={'User-Agent': USER_AGENT,
                     'Accept': 'text/html,application/json,text/plain;q=0.9'}) as client:
        for redirect in range(6):
            validate_url(url)
            if time.monotonic() > deadline:
                raise TimeoutError('Page request exceeded its time budget.')
            with client.stream('GET', url) as response:
                if response.is_redirect:
                    if redirect == 5 or not response.headers.get('location'):
                        raise ValueError('Too many redirects or missing destination.')
                    url = urljoin(url, response.headers['location'])
                    continue
                response.raise_for_status()
                mime = response.headers.get('content-type', '').split(';', 1)[0].lower()
                if mime not in allowed and not mime.endswith('+json'):
                    raise ValueError('Unsupported page type; public HTML/text/JSON only.')
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > MAX_BYTES:
                        raise ValueError('Page exceeds the 2 MiB download limit.')
                    if time.monotonic() > deadline:
                        raise TimeoutError('Page request exceeded its time budget.')
                    body.extend(chunk)
                return url, mime, body.decode(response.encoding or 'utf-8', errors='replace')
    raise ValueError('Page request did not return text.')


def normalise_aria_tables(raw):
    """Preserve standard ARIA table/grid rows as Markdown-compatible HTML tables."""
    from lxml import html, etree
    tree = html.fromstring(raw)
    changed = False
    for source in tree.xpath('//*[@role="table" or @role="grid"]'):
        if source.tag == 'table' or source.getparent() is None:
            continue
        rows = source.xpath('.//*[@role="row"]')
        table = html.Element('table')
        for row in rows:
            nearest = row.xpath('ancestor::*[@role="table" or @role="grid"][1]')
            if not nearest or nearest[0] is not source:
                continue
            cells = row.xpath('.//*[@role="cell" or @role="gridcell" or @role="columnheader" or @role="rowheader"]')
            tr = etree.SubElement(table, 'tr')
            for cell in cells:
                ancestors = cell.xpath('ancestor::*[@role="row"][1]')
                if not ancestors or ancestors[0] is not row:
                    continue
                td = etree.SubElement(tr, 'th' if cell.get('role') in ('columnheader', 'rowheader') else 'td')
                td.text = ' '.join(' '.join(cell.itertext()).split())
            if not len(tr):
                table.remove(tr)
        if len(table):
            table.tail = source.tail
            source.getparent().replace(source, table)
            changed = True
    return html.tostring(tree, encoding='unicode') if changed else raw


def extract_html(raw, url):
    from trafilatura import extract
    from lxml import html
    options = dict(url=url, include_links=True, include_tables=True,
                   include_comments=False, output_format='markdown')
    raw = normalise_aria_tables(raw)
    text = extract(raw, **options)
    if not text or len(text.strip()) < 200:
        recovered = extract(raw, favor_recall=True, **options)
        if recovered and len(recovered) > len(text or ''):
            text = recovered
    tree = html.fromstring(raw)
    dates = tree.xpath('//meta[@property="article:published_time" or '
                       '@property="article:modified_time" or @itemprop="datePublished"]/@content')
    if not dates:
        dates = tree.xpath('//time[@datetime]/@datetime')[:4]
    dates = [str(value)[:120] for value in dates[:4]]
    if text and text.strip():
        return text, dates
    blocks = []
    total = 0
    for node in tree.xpath('//script[@type="application/json" or @type="application/ld+json"]'):
        payload = (node.text or '').strip()
        if not payload or total + len(payload) > MAX_TEXT:
            continue
        try:
            json.loads(payload)
        except (ValueError, RecursionError):
            continue
        blocks.append(payload)
        total += len(payload)
    if blocks:
        return 'Embedded JSON metadata/state; verify it contains the requested facts.\n\n' + '\n\n'.join(blocks), dates
    return None, dates


def readability_markdown(raw, url):
    """An independent article extractor, using the same fetched HTML."""
    from readability import Document
    return full_markdown(Document(raw, url=url).summary(html_partial=True), url)


def full_markdown(raw, url):
    """Locally convert static HTML structure, including lists, tables and links."""
    from lxml import html
    from markdownify import markdownify
    tree = html.fromstring(normalise_aria_tables(raw), base_url=url)
    for node in tree.xpath('//script | //style | //template | //svg | //iframe | '
                           '//*[@hidden or @aria-hidden="true"]'):
        parent = node.getparent()
        if parent is not None:
            node.drop_tree()
    # lxml's base-href pass does not propagate handle_failures to its inner
    # rewrite. Discard malformed attributes before resolving the document.
    for node, attribute, href, _ in list(tree.iterlinks()):
        try:
            urlsplit(href)
        except ValueError:
            if attribute:
                node.attrib.pop(attribute, None)
    tree.make_links_absolute(url, resolve_base_href=True, handle_failures='discard')
    for node in tree.xpath('//a[@href] | //area[@href]'):
        if not link_shape(node.get('href', '')):
            node.attrib.pop('href', None)
    bodies = tree.xpath('//body')
    body = bodies[0] if bodies else tree
    return markdownify(html.tostring(body, encoding='unicode'), heading_style='ATX',
        table_infer_header=True, strip=['img'], bs4_options='lxml').strip()


def text_window(text, offset, budget, *, stop=None, line_limit=80):
    """Number lines and return an exact continuation offset, even inside a long line."""
    stop = len(text) if stop is None else min(len(text), stop)
    position = min(max(0, offset), stop)
    line = text.count('\n', 0, position) + 1
    parts, used = [], 0
    while position < stop and len(parts) < line_limit:
        newline = text.find('\n', position, stop)
        end = newline + 1 if newline >= 0 else stop
        prefix = f'L{line}: '
        room = budget - used - len(prefix) - 1
        if room <= 0:
            break
        end = min(end, position + room)
        chunk = text[position:end]
        parts.append(prefix + chunk.rstrip('\n'))
        used += len(prefix) + len(chunk) + 1
        position = end
        if chunk.endswith('\n'):
            line += 1
    return '\n'.join(parts), position


def find_windows(text, query, offset, budget, max_matches, context_lines=2):
    """Literal case-insensitive search in cached text; no model-generated summaries."""
    windows, remaining, last_end, more = [], budget, -1, False
    for match in re.compile(re.escape(query), re.IGNORECASE).finditer(text, offset):
        if match.start() < last_end:
            continue
        if len(windows) >= max_matches or remaining < 300:
            more = True
            break
        per_window = min(remaining, max(1000, budget // max_matches))
        start = text.rfind('\n', 0, match.start()) + 1
        for _ in range(context_lines):
            previous = text.rfind('\n', 0, max(0, start - 1))
            start = previous + 1 if previous >= 0 else 0
        desired_start = start
        # Long paragraph context must not push the matched phrase out of the budget.
        start = max(start, match.start() - per_window // 4)
        stop = match.end()
        for _ in range(context_lines + 1):
            following = text.find('\n', stop)
            stop = following + 1 if following >= 0 else len(text)
        content, end = text_window(text, start, per_window, stop=stop)
        window = {'line': text.count('\n', 0, match.start()) + 1,
                  'offset': start, 'content': content}
        if start > desired_start or end < stop:
            window['context_truncated'] = True
        windows.append(window)
        remaining -= len(content)
        last_end = end
    result = {'matches': windows}
    if more:
        result['next_offset'] = last_end
    if not windows:
        result['warning'] = 'No match in the extracted text. Try mode=full or another source; absence here is not proof.'
    return result


def page_result(page, budget, links_limit=0, links_query='',
                 action='read', offset=0, query='', max_matches=3, context_lines=2):
    text = page['_text']
    result = {'success': True, 'ref': remember_url(page['url']), 'url': page['url'],
              'total_lines': text.count('\n') + 1,
              'version': page_version(page)}
    if page.get('source_dates'):
        result['source_dates'] = page['source_dates']
    if page.get('provider'):
        result['provider'] = page['provider']
    if page.get('render_warning'):
        result['render_warning'] = page['render_warning']
    if page.get('stored_truncated'):
        result['source_truncated'] = True
    if not page.get('text_available', True):
        result['text_available'] = False
    if action == 'read':
        content, end = text_window(text, offset, budget, line_limit=max(80, budget // 75))
        result.update(offset=offset, content=content)
        if end < len(text):
            result['next_offset'] = end
    elif action == 'find':
        result.update(find_windows(text, query, offset, budget, max_matches, context_lines))
    links = page.get('_links', [])
    if links_query:
        term = links_query.casefold()
        links = [link for link in links if term in (link['title'] + ' ' + link['url']).casefold()]
    link_offset = offset if action == 'links' else 0
    visible = links[link_offset:link_offset + links_limit]
    # Cached reads re-register refs. Only source identity and link labels go to the model.
    if visible or action == 'links':
        result['links'] = [{'ref': remember_url(link['url']), 'title': link['title'],
                            'url': link['url']} for link in visible]
        if link_offset + len(visible) < len(links):
            result['next_links_offset'] = link_offset + len(visible)
    if page.get('_links_truncated'):
        result['links_incomplete'] = True
    return result



def page_version(page):
    # Include extracted links: pagination must not silently cross page versions.
    raw = encode_result([page['url'], page['_text'], page.get('_links', [])])
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def response_bytes(value):
    return len(encode_result(value).encode('utf-8'))


def page_payload(page, budget, links_limit=0, links_query='',
                 action='read', offset=0, query='', max_matches=3, context_lines=2):
    """Bound the whole reply, not just body text; preserve exact continuation."""
    ceiling = min(10000, max(4096, budget * 2))
    while True:
        result = page_result(page, budget, links_limit, links_query,
                             action, offset, query, max_matches, context_lines)
        result['untrusted'] = True
        if response_bytes(result) <= ceiling:
            return encode_result(result)
        # Link rows are atomic. Keep URLs intact and expose the next index.
        if links_limit > 1:
            links_limit -= 1
        elif action == 'links':
            raise ValueError('A link exceeds the response budget; refine links_query.')
        elif budget > 300:
            budget = max(300, budget * 3 // 4)
        elif links_limit:
            links_limit = 0
        else:
            raise ValueError('Page metadata exceeds the response budget.')


def search_payload(output):
    """Keep source identity, dates and stable offsets, omit repeated diagnostics."""
    output.pop('notice', None)
    output.pop('engine', None)
    output.pop('provider', None)
    output['untrusted_snippets'] = True
    if output.get('warnings'):
        output['warnings'] = list(dict.fromkeys(output['warnings']))[:3]
    else:
        output.pop('warnings', None)
    rows = output['data']['web']
    # Even 10 results with long URLs must not flood a model turn.
    while len(rows) > 1 and response_bytes(output) > 6000:
        rows.pop()
        output['next'] = {'page': output['page'], 'offset': output['offset'] + len(rows)}
        output['returned'] = len(rows)
    if response_bytes(output) > 6000:
        raise ValueError('Search result exceeds the response budget.')
    return encode_result(output)


def read_page(args, **kwargs):
    try:
        supplied_url = str(args.get('url', '')).strip()
        supplied_ref = str(args.get('ref', '')).strip()
        if bool(supplied_url) == bool(supplied_ref):
            raise ValueError('Provide exactly one url or returned ref.')
        requested_url = resolve_ref(supplied_ref) if supplied_ref else supplied_url
        validate_url(requested_url)
        budget = max(1000, min(6000, int(args.get('char_limit', 2400))))
        query = str(args.get('query', '')).strip()
        action = str(args.get('action', 'find' if query else 'read'))
        if action not in ('read', 'find', 'links'):
            raise ValueError('action must be read, find, or links.')
        offset = int(args.get('offset', 0))
        if not 0 <= offset <= MAX_TEXT:
            raise ValueError('offset must be between 0 and 200000.')
        if query and action != 'find':
            raise ValueError('query searches source text: use action=find or omit action.')
        if action == 'find' and not 1 <= len(query) <= 200:
            raise ValueError('find requires a literal query of 1–200 characters.')
        max_matches = max(1, min(5, int(args.get('max_matches', 3))))
        context_lines = int(args.get('context_lines', 2))
        if not 0 <= context_lines <= 20:
            raise ValueError('context_lines must be between 0 and 20.')
        default_links = 6 if action == 'links' else 0
        links_limit = max(0, min(MAX_LINKS, int(args.get('links_limit', default_links))))
        if action == 'links' and not links_limit:
            raise ValueError('links_limit must be positive for action=links.')
        links_query = str(args.get('links_query', '')).strip()[:200]
        mode = str(args.get('mode', 'auto'))
        if mode not in ('auto', 'full'):
            raise ValueError('mode must be auto or full.')
        backend = str(args.get('backend', 'auto'))
        if backend not in ('auto', 'local') + FREE_PROVIDERS:
            raise ValueError('Invalid reading backend.')
        render = str(args.get('render', 'auto'))
        if render not in ('auto', 'always', 'never'):
            raise ValueError('render must be auto, always or never.')
        version = args.get('version')
        if version is not None and (not isinstance(version, str) or not re.fullmatch(r'[0-9a-f]{12}', version)):
            raise ValueError('Use the returned page version.')
        cache_key = ('read', mode, backend, render, requested_url)
        refresh = args.get('refresh', False)
        if isinstance(refresh, str) and refresh.lower() in ('true', 'false'):
            refresh = refresh.lower() == 'true'
        if not isinstance(refresh, bool):
            raise ValueError('refresh must be true or false.')
        if version is not None:
            if refresh:
                raise ValueError('A pinned snapshot cannot be refreshed. Read without version first.')
            page = cached_snapshot(requested_url, version)
            if page is None:
                raise ValueError('Page snapshot changed or expired. Read from offset=0 without version.')
            return page_payload(page, budget, links_limit, links_query,
                                action, offset, query, max_matches, context_lines)
        if not refresh:
            page = cached_page(cache_key)
            if page is not None:
                return page_payload(page, budget, links_limit, links_query,
                                    action, offset, query, max_matches, context_lines)
        if backend in FREE_PROVIDERS:
            page = free_extract(requested_url, backend)
            cache_page(cache_key, page)
            return page_payload(page, budget, links_limit, links_query,
                                action, offset, query, max_matches, context_lines)
        try:
            url, mime, raw = fetch_public(requested_url)
        except Exception:
            if backend == 'local':
                raise
            page = free_extract(requested_url)
            cache_page(cache_key, page)
            return page_payload(page, budget, links_limit, links_query,
                                action, offset, query, max_matches, context_lines)
        source_dates = []
        links = []
        links_truncated = False
        text_available = True
        javascript_present = False
        browser_engine = None
        render_warning = None
        if 'html' in mime:
            # Keep the existing rendered-page behavior by default. A static shell
            # can contain plenty of navigation text while omitting its real data.
            if render != 'never':
                try:
                    url, raw = render_public(url)
                    browser_engine = 'local_renderer'
                except (ValueError, OSError) as error:
                    render_warning = 'Static HTML fallback: ' + str(error)[:160]
            from lxml import html
            tree = html.fromstring(raw)
            javascript_present = bool(tree.xpath('//script[not(@type="application/json" or @type="application/ld+json")]'))
            links, links_truncated = extract_links(raw, url)
            text, source_dates = extract_html(raw, url)
            embedded_json = bool(text and text.startswith('Embedded JSON'))
            if mode == 'full':
                text = full_markdown(raw, url)
            elif len(text or '') < 200:
                try:
                    recovered = readability_markdown(raw, url)
                    if len(recovered) > len(text or ''):
                        text = recovered
                except (ValueError, TypeError):
                    pass
            if len(text or '') < 200 and not embedded_json:
                recovered = full_markdown(raw, url)
                if len(recovered) > len(text or ''):
                    text = recovered
            if not text:
                if links:
                    text_available = False
                    text = 'No readable static text. Discovered links are available.'
                else:
                    if backend == 'auto':
                        page = free_extract(requested_url)
                        cache_page(cache_key, page)
                        return page_payload(page, budget, links_limit, links_query,
                                            action, offset, query, max_matches, context_lines)
                    return encode_result({'success': False, 'url': url,
                        'error_code': 'no_static_content',
                        'javascript_present': javascript_present,
                        'render_warning': render_warning,
                        'error': 'No readable content after page reading.'})
        else:
            if mime == 'application/json' or mime.endswith('+json'):
                text = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
            elif mime.endswith('xml'):
                from lxml import etree
                parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
                tree = etree.fromstring(raw.encode('utf-8'), parser=parser)
                text = etree.tostring(tree, encoding='unicode', pretty_print=True)
            else:
                text = raw
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        page = {'url': url, '_text': text[:MAX_TEXT],
            'stored_truncated': len(text) > MAX_TEXT,
            '_links': links, '_links_truncated': links_truncated,
            'text_available': text_available,
            'source_dates': source_dates,
            'browser_engine': browser_engine,
            'render_warning': render_warning,
            'javascript_present': javascript_present and not text_available}
        cache_page(cache_key, page)
        if requested_url != url:
            cache_page(('read', mode, backend, render, url), page)
        return page_payload(page, budget, links_limit, links_query,
                            action, offset, query, max_matches, context_lines)
    except Exception as error:
        return encode_result({'success': False, 'error': str(error)[:500]})


"""One public image per call, fetched and decoded entirely in memory."""
IMAGE_MAX_BYTES = 6 * 1024 * 1024
IMAGE_MAX_PIXELS = 8_000_000
_image_lock = threading.Lock()


def fetch_image(url):
    from tools.url_safety import create_ssrf_safe_client
    import httpx
    deadline = time.monotonic() + 25
    with create_ssrf_safe_client(timeout=httpx.Timeout(12, connect=8),
            follow_redirects=False, trust_env=False,
            headers={'User-Agent': USER_AGENT, 'Accept': 'image/*'}) as client:
        for redirect in range(6):
            validate_url(url)
            with client.stream('GET', url) as response:
                if time.monotonic() > deadline:
                    raise TimeoutError('Image request exceeded its time budget.')
                if response.is_redirect:
                    location = response.headers.get('location')
                    if redirect == 5 or not location:
                        raise ValueError('Too many redirects or missing destination.')
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                mime = response.headers.get('content-type', '').split(';', 1)[0].lower()
                if mime and not (mime.startswith('image/') or mime == 'application/octet-stream'):
                    raise ValueError('Use a direct image URL, not an HTML page.')
                length = response.headers.get('content-length')
                if length and int(length) > IMAGE_MAX_BYTES:
                    raise ValueError('Image exceeds the 6 MiB download limit.')
                body = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() > deadline:
                        raise TimeoutError('Image request exceeded its time budget.')
                    if len(body) + len(chunk) > IMAGE_MAX_BYTES:
                        raise ValueError('Image exceeds the 6 MiB download limit.')
                    body.extend(chunk)
                return url, bytes(body)
    raise ValueError('No image returned.')


def image_data(raw):
    import io
    from PIL import Image, ImageOps

    class BoundedBuffer(io.BytesIO):
        def write(self, data):
            if self.tell() + len(data) > IMAGE_MAX_BYTES:
                raise ValueError('Decoded image exceeds the 6 MiB output limit.')
            return super().write(data)

    with Image.open(io.BytesIO(raw)) as image:
        width, height = image.size
        if not 0 < width * height <= IMAGE_MAX_PIXELS:
            raise ValueError('Image exceeds the 8 megapixel decode limit.')
        if image.format not in ('JPEG', 'PNG', 'WEBP', 'AVIF', 'GIF', 'BMP', 'TIFF'):
            raise ValueError('Use a raster image: PNG, JPEG, WebP, AVIF, GIF, BMP or TIFF.')
        original_format = image.format
        animated = getattr(image, 'n_frames', 1) > 1
        orientation = image.getexif().get(274, 1)
        image.seek(0)
        image.load()
        if original_format in ('PNG', 'JPEG') and orientation == 1 and not animated:
            return raw, 'image/png' if original_format == 'PNG' else 'image/jpeg', width, height, False
        with ImageOps.exif_transpose(image) as corrected:
            with corrected.convert('RGBA') as normalized, BoundedBuffer() as output:
                normalized.save(output, format='PNG')
                return output.getvalue(), 'image/png', normalized.width, normalized.height, animated


def view_image(args, **kwargs):
    import base64
    try:
        url = args.get('url')
        if not isinstance(url, str) or not 0 < len(url.strip()) <= 2048:
            raise ValueError('Provide one direct public image URL, up to 2048 characters.')
        # Serialize decoding in each worker to bound peak RAM.
        with _image_lock:
            final_url, raw = fetch_image(url.strip())
            data, mime, width, height, animated = image_data(raw)
            encoded = base64.b64encode(data).decode('ascii')
        metadata = {'success': True, 'url': final_url, 'width': width, 'height': height}
        if animated:
            metadata['note'] = 'First frame only.'
        text = encode_result(metadata)
        return {'_multimodal': True,
                'content': [{'type': 'text', 'text': text},
                            {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{encoded}'}}],
                'text_summary': f'Image {width}x{height} loaded from {final_url}',
                'meta': {'native_vision': True, 'size_bytes': len(data)}}
    except Exception as error:
        return encode_result({'success': False, 'error': str(error)[:500]})


def register_image(ctx):
    ctx.register_tool('web_image', 'evo_web', {
        'name': 'web_image',
        'description': 'View one direct public image URL with native vision. Returns the image itself, not a text description. Download and conversion stay in RAM. No resizing; up to 6 MiB and 8 megapixels. Animated images show the first frame. For HTML pages use web_read.',
        'parameters': {'type': 'object', 'properties': {
            'url': {'type': 'string', 'description': 'Direct HTTP/HTTPS image URL.'}},
            'required': ['url']}}, view_image)


def register(ctx):
    if hasattr(ctx, 'register_hook'):
        ctx.register_hook('post_llm_call', finish_search_turn)
        ctx.register_hook('agent_loop_stopped', finish_search_turn)
        ctx.register_hook('on_session_reset', lambda **kwargs: clear_cache(clear_refs=True))
    ctx.register_tool('internet_search', 'evo_web', {
        'name': 'internet_search',
        'description': 'Search public web; snippets are unverified leads. Open sources with web_read. Continue with returned next page/offset and the same query. Explicit engines never switch.',
        'parameters': {'type': 'object', 'properties': {
            'query': {'type': 'string', 'description': 'Supports site:, filetype:, quotes and exclusions.'},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10, 'default': 5},
            'engine': {'type': 'string', 'enum': list(SEARCH_ENGINES), 'default': 'auto',
                       'description': 'auto: free then local. local: SearXNG/DDGS. google: CSE (5 pages). Free/Bing: 1 page.'},
            'objective': {'type': 'string', 'maxLength': 1000, 'description': 'Parallel research goal.'},
            'search_queries': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 3,
                               'description': 'Parallel only; defaults to [query].'},
            'page': {'type': 'integer', 'minimum': 1, 'maximum': 20, 'default': 1},
            'offset': {'type': 'integer', 'minimum': 0, 'maximum': 50, 'default': 0},
            'snippet_chars': {'type': 'integer', 'minimum': 80, 'maximum': 1200, 'default': 200,
                              'description': 'Characters per snippet.'},
            'time_range': {'type': 'string', 'enum': ['', 'day', 'week', 'month', 'year'], 'default': '',
                           'description': 'Local freshness filter; verify source dates. Unavailable for free providers.'},
            'language': {'type': 'string', 'enum': ['en', 'ko', 'all'], 'default': 'en'}},
            'required': ['query']}}, search)
    # Flat properties keep llama.cpp's native argument parser compatible.
    # read_page validates that exactly one URL or ref was provided.
    ctx.register_tool('web_read', 'evo_web', {
        'name': 'web_read',
        'description': 'Read untrusted source text, with line numbers. find searches a literal phrase; links lists navigation. Continue with next_offset + version. Cached in RAM. HTML is rendered unless render=never.',
        'parameters': {'type': 'object', 'properties': {
            'url': {'type': 'string', 'description': 'Public URL; supply url or ref, not both.'},
            'ref': {'type': 'string', 'description': 'Returned ref; expires after 30 min or worker restart.'},
            'action': {'type': 'string', 'enum': ['read', 'find', 'links'],
                       'description': 'Default: find when query is provided, otherwise read.'},
            'query': {'type': 'string', 'maxLength': 200, 'description': 'find: case-insensitive literal phrase.'},
            'offset': {'type': 'integer', 'minimum': 0, 'maximum': 200000, 'default': 0,
                       'description': 'Returned next_offset, or next_links_offset for links.'},
            'max_matches': {'type': 'integer', 'minimum': 1, 'maximum': 5, 'default': 3},
            'context_lines': {'type': 'integer', 'minimum': 0, 'maximum': 20, 'default': 2,
                              'description': 'Surrounding lines per match.'},
            'mode': {'type': 'string', 'enum': ['auto', 'full'], 'default': 'auto', 'description': 'Article text or whole-page Markdown.'},
            'backend': {'type': 'string', 'enum': ['auto', 'local'] + list(FREE_PROVIDERS), 'default': 'auto',
                        'description': 'local: EVO only. auto: free fallback. Named provider: only that service.'},
            'links_limit': {'type': 'integer', 'minimum': 0, 'maximum': 60,
                            'description': 'Default: read/find 0, links 6.'},
            'links_query': {'type': 'string', 'description': 'Filter link title/URL by substring.'},
            'refresh': {'type': 'boolean', 'default': False},
            'version': {'type': 'string', 'description': 'Pins a returned snapshot. mode/backend/render apply only without version.'},
            'render': {'type': 'string', 'enum': ['auto', 'always', 'never'], 'default': 'auto'},
            'char_limit': {'type': 'integer', 'minimum': 1000, 'maximum': 6000, 'default': 2400, 'description': 'Body budget; whole reply also bounded. Continue using offsets.'}},
            }}, read_page)

    register_image(ctx)
