"""Presentation checks without Discord messages or model requests."""
import asyncio

from evo_omp.discord_text import messages, readable_tables, units


def main():
    sample = '**확인 사실** | 항목 | 값 | |---|---| | 버전 (`Makefile`) | `7.3.0-rc7`, NAME `Baby Opossum Posse` |'
    expected = '**확인 사실**\n항목 / 값\n- 버전 (`Makefile`): `7.3.0-rc7`, NAME `Baby Opossum Posse`'
    assert readable_tables(sample) == expected
    print('PASS supplied collapsed table becomes a readable labeled list')

    sample = '**Facts**\n\n| Model | Speed | RAM |\n|:---|---:|:---:|\n| A | 20 tok/s | 8 GiB |\n| B | 30 tok/s | 9 GiB |\n\nEnd.'
    result = readable_tables(sample)
    assert 'Model / Speed / RAM' in result and '- A\n  - Speed: 20 tok/s\n  - RAM: 8 GiB' in result
    assert '- B\n  - Speed: 30 tok/s\n  - RAM: 9 GiB' in result and result.endswith('\n\nEnd.')
    assert '|:---|' not in result
    assert readable_tables('Key | Value\n--- | ---\nx | y') == 'Key / Value\n- x: y'
    assert readable_tables('|Key|Value|\n|---|---|\n|x||') == 'Key / Value\n- x: '
    print('PASS normal tables preserve headers, values, alignment and surrounding text')

    code = '```markdown\n| Key | Value |\n|---|---|\n| x | y |\n```'
    assert readable_tables(code) == code
    assert readable_tables('Use `a|b`, or x | y.\nNo separator here.') == 'Use `a|b`, or x | y.\nNo separator here.'
    result = readable_tables('| Key | Value |\n|---|---|\n| `a|b` | x\\|y |')
    assert '- `a|b`: x\\|y' in result
    print('PASS fenced examples, inline pipes and escaped pipe values survive')

    text = 'first\n' + 'line\n' * 1000 + 'last'
    parts = messages(text)
    assert ''.join(parts) == text and all(units(p) <= 1900 for p in parts)
    assert all(p.endswith('\n') for p in parts[:-1])
    text = '😀' * 4000
    parts = messages(text)
    assert ''.join(parts) == text and all(units(p) <= 1900 for p in parts)
    print('PASS whole-line splits and long emoji text stay under Discord UTF-16 limit')

    text = '```python\n' + "print('hello')\n" * 500 + '```\nDone.'
    parts = messages(text)
    assert len(parts) > 1 and all(units(p) <= 1900 for p in parts)
    assert all(p.startswith('```python\n') and p.count('```') == 2 for p in parts)
    assert ''.join(parts).count("print('hello')") == 500 and parts[-1].endswith('Done.')
    assert all(p.rstrip().endswith('```') for p in parts[:-1])
    print('PASS long fenced code closes and reopens with the same language')

    for length in (1, 1850, 1890, 1895, 1900, 1901, 5000):
        text = '````text\n' + '😀' * length
        parts = messages(text)
        assert all(units(p) <= 1900 for p in parts)
        assert sum(p.count('😀') for p in parts) == length
        assert all(p.startswith('````text\n') and p.rstrip().endswith('````') for p in parts)
    assert messages('') == []
    assert messages('**short**\n- item') == ['**short**\n- item']
    print('PASS unmatched fences, longer markers and boundary sizes keep complete content')
    print('EXAMPLE\n' + expected)


async def delivery():
    from evo_omp.gateway import Bot
    from evo_omp.integrations.codex import Bridge

    class Channel:
        def __init__(self): self.sent = []
        async def send(self, text, **kwargs): self.sent.append((text, kwargs))

    channel = Channel()
    sample = '| Key | Value |\n|---|---|\n| x | y |\n\n```python\n' + 'print(1)\n' * 600 + '```'
    assert await Bot.deliver(None, channel, sample)
    assert channel.sent[0][0].startswith('Key / Value\n- x: y\n')
    assert sum(s.count('print(1)') for s, _ in channel.sent) == 600
    assert all(units(s) <= 1900 for s, _ in channel.sent)
    assert all(not opts['allowed_mentions'].everyone for _, opts in channel.sent)
    print('PASS actual gateway uses formatting while preserving mention suppression')

    captured = []
    bridge = Bridge.__new__(Bridge)
    async def send(job, text, **kwargs): captured.append((text, kwargs))
    bridge.send = send
    table = '| Key | Value |\n|---|---|\n| x | y |'
    job = dict(turn_id='table')
    notice = dict(job_id='captured', turn_id='table', state='completed', answer=table)
    await bridge.notify_complete(job, notice)
    assert captured[-1][0].endswith('Key / Value\n- x: y')
    raw = table + '\n' + '😀' * 4000
    notice.update(turn_id='long', answer=raw)
    job['turn_id'] = 'long'
    await bridge.notify_complete(job, notice)
    text, opts = captured[-1]
    try:
        assert units(text) < 2000 and 'Key / Value\n- x: y' in text
        assert opts['file'].fp.getvalue().decode() == raw
    finally:
        opts['file'].close()
    print('PASS Codex notifications format tables and preserve the full raw attachment')


if __name__ == '__main__':
    main()
    try:
        import discord
    except ModuleNotFoundError:
        print('SKIP gateway boundary: local Discord dependency absent; run on EVO')
    else:
        asyncio.run(delivery())
