"""Small Discord presentation adapter: pipe tables to lists, fenced chunks."""
import re


def cells(line):
    values, value, ticks = [], '', ''
    for token in re.findall(r'\\.|`+|\||[^\\`|]+|\\$', line.strip()):
        if token.startswith('`'):
            ticks = '' if token == ticks else token if not ticks else ticks
        if token == '|' and not ticks:
            values.append(value.strip())
            value = ''
        else:
            value += token
    values.append(value.strip())
    if line.strip().startswith('|'):
        values.pop(0)
    if line.strip().endswith('|') and not values[-1]:
        values.pop()
    return values


def rows_as_list(headers, rows):
    output = [' / '.join(headers)]
    for row in rows:
        if len(row) == 1:
            output.append('- ' + row[0])
        elif len(row) == 2:
            output.append('- ' + row[0] + ': ' + row[1])
        else:
            output.append('- ' + row[0])
            output.extend('  - ' + (header or '값') + ': ' + value
                          for header, value in zip(headers[1:], row[1:]))
    return output


def fence_after(line, fence):
    marker = re.fullmatch(r' {0,3}(`{3,16})([^`\n]*)', line.rstrip('\r\n'))
    if marker is None:
        return fence
    if fence:
        return None if len(marker[1]) >= len(fence[0]) and not marker[2].strip() else fence
    return (marker[1], marker[0].strip()) if len(marker[0]) <= 120 else None


def readable_tables(text):
    lines, output, index, fence = text.splitlines(), [], 0, None
    while index < len(lines):
        line = lines[index]
        next_fence = fence_after(line, fence)
        if fence or next_fence:
            output.append(line)
            fence = next_fence
            index += 1
            continue
        # Accept collapsed table rows only when a full separator and consistent
        # column counts make the boundary unambiguous enough to preserve values.
        inline = re.search(r'\|(?:[ \t]*:?-{3,}:?[ \t]*\|){2,}', line)
        if inline:
            count = len(cells(inline[0]))
            before = cells(line[:inline.start()])
            rows = [cells(row) for row in re.split(r'\|[ \t]+\|', line[inline.end():].strip())]
            if len(before) in (count, count + 1) and rows and all(len(r) == count for r in rows):
                if len(before) > count:
                    output.append(before[0])
                output.extend(rows_as_list(before[-count:], rows))
                index += 1
                continue
        headers = cells(line)
        separator = cells(lines[index + 1]) if index + 1 < len(lines) else []
        if ('|' in line and len(headers) == len(separator) and separator
                and all(re.fullmatch(r':?-{3,}:?', cell) for cell in separator)):
            end, rows = index + 2, []
            while end < len(lines) and '|' in lines[end] and not fence_after(lines[end], None):
                row = cells(lines[end])
                if len(row) != len(headers):
                    break
                rows.append(row)
                end += 1
            if rows:
                output.extend(rows_as_list(headers, rows))
                index = end
                continue
        output.append(line)
        index += 1
    return '\n'.join(output)


def units(text):
    return len(text.encode('utf-16-le')) // 2


def messages(text, limit=1900):
    """Keep whole lines where possible; close/reopen code fences at each split."""
    if limit < 256:
        raise ValueError('Discord chunk limit must be at least 256')
    output, buffer, fence = [], '', None

    def flush():
        nonlocal buffer
        if buffer:
            output.append(buffer + ('\n' + fence[0] if fence else ''))
        buffer = fence[1] + '\n' if fence else ''

    for line in readable_tables(text).splitlines(keepends=True):
        next_fence = fence_after(line, fence)
        closing = 1 + len(next_fence[0]) if next_fence else 0
        if units(buffer + line) + closing > limit and buffer:
            flush()
        # Fence lines are bounded by fence_after; only content needs a hard cut.
        while units(buffer + line) + closing > limit:
            room = limit - units(buffer) - (1 + len(fence[0]) if fence else 0)
            part = line.encode('utf-16-le')[:room * 2].decode('utf-16-le', errors='ignore')
            buffer += part
            line = line[len(part):]
            flush()
        buffer += line
        fence = next_fence
    if buffer:
        flush()
    return output
