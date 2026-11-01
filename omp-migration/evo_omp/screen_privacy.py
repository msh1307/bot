"""Best-effort local OCR masking for model-bound browser/RDP screenshots only.

No image/OCR files, external service or LLM sees the unfiltered frame. OCR can
miss text; this supplements browser field masks, not a secrecy guarantee.
"""
import base64
import csv
import io
from pathlib import Path
import re
import subprocess

from PIL import Image, ImageDraw

ROOT = Path('/opt/evo-omp/screen-ocr')
TOKEN = re.compile(r'(?i)(?:gh[pousr]_[a-z0-9_]{15,}|github_pat_[a-z0-9_]{15,}|'
                   r'evo_[a-z0-9_-]{20,}|sk-[a-z0-9_-]{20,}|'
                   r'[a-z0-9_-]{20,}\.[a-z0-9_-]{6,}\.[a-z0-9_-]{20,})')
LABEL = re.compile(r'(?i)\b(?:password|passwd|pwd|secret|api[ _-]?key|'
                   r'(?:access|refresh|auth)[ _-]?token|totp[ _-]?(?:seed|secret)|'
                   r'otp|verification[ _-]?code)\s*[:=]\s*\S+')


def known_values():
    # Private files remain outside the workspace and never enter the OCR argv.
    from . import context
    from .integrations import credential_input
    values = list(context.secret_values)
    data = credential_input.account_document()
    paths, otp_paths = set(), set()
    for entry in data.get('accounts', {}).values():
        paths.update(entry[field] for field in ('password_file', 'sudo_password_file', 'totp_file') if entry.get(field))
        if entry.get('totp_file'):
            otp_paths.add(entry['totp_file'])
    for entry in data.get('instances', {}).values():
        if entry.get('codex', {}).get('token_file'):
            paths.add(entry['codex']['token_file'])
    for path in sorted(paths):
        value = credential_input.private_text(path).rstrip('\r\n')
        values.append(value)
        if path in otp_paths:
            import time
            values.extend(credential_input.totp(value, time.time() + n * 30) for n in (-1, 0, 1))
    return [fold(v) for v in values if isinstance(v, str) and fold(v)]


def fold(text):
    # Ignore OCR-inserted spaces/punctuation and common Latin OCR confusions.
    return ''.join(c for c in text.casefold() if c.isalnum()).translate(str.maketrans('01i', 'oll'))


def ocr(image):
    source = io.BytesIO()
    image.save(source, 'PNG')
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'OMP_THREAD_LIMIT': '1',
           'OMP_NUM_THREADS': '1', 'LD_LIBRARY_PATH': str(ROOT / 'usr/lib/x86_64-linux-gnu')}
    result = subprocess.run(['/usr/bin/prlimit', '--core=0:0', '--',
        str(ROOT / 'usr/bin/tesseract'), 'stdin', 'stdout',
        '--tessdata-dir', str(ROOT / 'usr/share/tesseract-ocr/5/tessdata'),
        '-l', 'eng', '--psm', '11', '-c', 'debug_file=/dev/null', 'tsv'],
        input=source.getvalue(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=5, check=True, env=env)
    if len(result.stdout) > 2**20 or not result.stdout.startswith(b'level\tpage_num\t'):
        raise ValueError('OCR output limit')
    lines = {}
    for row in csv.DictReader(io.StringIO(result.stdout.decode('utf-8')), delimiter='\t'):
        if row['level'] != '5' or not row['text'].strip():
            continue
        key = tuple(row[k] for k in ('page_num', 'block_num', 'par_num', 'line_num'))
        x, y, w, h = (int(row[k]) for k in ('left', 'top', 'width', 'height'))
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x+w > image.width or y+h > image.height:
            raise ValueError('Invalid OCR coordinates')
        lines.setdefault(key, []).append((row['text'], (x, y, x+w, y+h)))
    return lines.values()


def filter_result(result):
    """Only filtered pixels may leave; failures never return the original image."""
    if not isinstance(result, dict) or not any(b.get('type') == 'image' for b in result.get('content', [])):
        return result
    try:
        values = known_values()
        blocks = []
        for block in result['content']:
            if block.get('type') != 'image':
                blocks.append(block)
                continue
            raw = base64.b64decode(block['data'], validate=True)
            if len(raw) > 450000:
                raise ValueError('Image byte limit')
            with Image.open(io.BytesIO(raw)) as source:
                if source.width * source.height > 1280 * 720:
                    raise ValueError('Image pixel limit')
                image = source.convert('RGB')
            draw = ImageDraw.Draw(image)
            masked = False
            for words in ocr(image):
                text = ' '.join(word for word, _ in words)
                normalized = fold(text)
                if not (TOKEN.search(text) or TOKEN.search(''.join(word for word, _ in words))
                        or LABEL.search(text) or any(v in normalized for v in values)):
                    continue
                # Mask the whole recognized line, including OCR gaps and punctuation.
                x0, y0 = min(b[0] for _, b in words), min(b[1] for _, b in words)
                x1, y1 = max(b[2] for _, b in words), max(b[3] for _, b in words)
                draw.rectangle((max(0, x0-8), max(0, y0-5), min(image.width-1, x1+8),
                                min(image.height-1, y1+5)), fill='black')
                masked = True
            if masked:
                output = io.BytesIO()
                image.save(output, 'JPEG', quality=65)
                if output.tell() > 450000:
                    raise ValueError('Filtered image byte limit')
                block = dict(block, data=base64.b64encode(output.getvalue()).decode(), mimeType='image/jpeg')
            blocks.append(block)
        return dict(result, content=blocks)
    except Exception:
        # OCR/config/library errors can contain secrets: never forward their text.
        raise ValueError('Screen privacy filter failed; no screenshot returned') from None
