"""Byte-exact previews, bounds, retention and failure behavior; no model needed."""
import json
from pathlib import Path
import re
import tempfile
from unittest.mock import patch

from evo_omp import tool_output as output


def run():
    with tempfile.TemporaryDirectory() as folder:
        profile = Path(folder)
        root = profile / 'sessions' / 'host-output'
        root.parent.mkdir()
        text = ''.join(f'{i:04d} 한글 UTF-8 exact source line\n' for i in range(1800))
        original = {'success': False, 'exit_code': 17, 'error': 'compiler failed', 'output': text}
        value = {'content': [{'type': 'text', 'text': json.dumps(original, ensure_ascii=False)}],
                 'isError': True, 'details': {'request_id': 'keep'}}
        result = output.compact_result('host_command', value, profile)
        small = result['content'][0]['text']
        parsed = json.loads(small)
        assert all(parsed[k] == original[k] for k in ('success', 'exit_code', 'error'))
        assert len(small.encode()) <= output.INLINE_BYTES
        assert result['isError'] and result['details'] == value['details']
        paths = list(root.glob('*.txt'))
        assert len(paths) == 1 and paths[0].read_text() == text
        assert 'middle omitted' in small and str(paths[0].name) in small
        assert output.compact_result('host_command', value, profile) == result
        assert len(list(root.iterdir())) == 1  # identical results share storage
        assert value['content'][0]['text'] == json.dumps(original, ensure_ascii=False)
        for tool in ('web_read', 'internet_search', 'memory', 'user_input'):
            assert output.compact_result(tool, value, profile) is value
        image = {'type': 'image', 'data': 'pixel-data', 'mimeType': 'image/png'}
        mixed = dict(value, content=[*value['content'], image])
        assert output.compact_result('browser', mixed, profile)['content'][1] is image
        # Any JSON fallback stays complete and parseable in its source file.
        many = json.dumps([{'id': i, 'value': 'x' * 250} for i in range(100)])
        limited = output.compact_text(many, root)
        name = re.search(r'/host-output/([a-f0-9]+\.txt)', limited)[1]
        assert (root / name).read_text() == many
        # Long single lines and partial multibyte edges are preserved in the file.
        long = '🙂' * 8000 + 'LAST'
        limited = output.compact_text(long, root)
        assert '\ufffd' not in limited
        assert any(p.read_text() == long for p in root.iterdir())
        with patch.object(output, 'MAX_FILES', 3), patch.object(output, 'MAX_BYTES', 25000):
            for i in range(10):
                output.compact_text(str(i) + 'x' * 9000, root)
            assert len(list(root.iterdir())) <= 3
            assert sum(p.stat().st_size for p in root.iterdir()) <= 25000
        # Symlink directories are rejected before write/retention cleanup.
        other = profile / 'other'; other.mkdir()
        bad = profile / 'bad'; bad.symlink_to(other, target_is_directory=True)
        try:
            output.save_text(bad, 'no write')
            raise AssertionError('symlink accepted')
        except ValueError:
            assert not list(other.iterdir())
        print('PASS exact UTF-8/source, error metadata, image preservation, bounded retention, symlink rejection')


if __name__ == '__main__': run()
