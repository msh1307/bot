// Bounded text pages. No shell startup, model request, or whole-file buffering.
'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
const MAX_BYTES = 16 * 1024 * 1024, MAX_CHARS = 12000;

async function readTextPage(args) {
  const file = args.path, start = args.start_line ?? 1, limit = args.limit ?? 200;
  if (typeof file !== 'string' || file.length > 4096 || file.includes('\0') || !path.isAbsolute(file))
    throw new Error('Absolute file path required');
  if (!Number.isInteger(start) || start < 1 || start > 1000000 ||
      !Number.isInteger(limit) || limit < 1 || limit > 200) throw new Error('Invalid text page');
  const handle = await fs.open(file, 'r');
  try {
    const stat = await handle.stat();
    if (!stat.isFile() || stat.size > MAX_BYTES) throw new Error('Text file required, at most 16 MiB');
    const decoder = new TextDecoder('utf-8', {fatal: true});
    const buffer = Buffer.alloc(65536);
    let offset = 0, line = 1, text = '', current = '', end = start - 1;
    const report = (more, partial = false) => ({path: file, start_line: start, end_line: end,
      next_line: more && !partial ? end + 1 : null, text, truncated: more,
      partial_line: partial, size_bytes: stat.size});
    while (offset < stat.size) {
      const {bytesRead} = await handle.read(buffer, 0, Math.min(buffer.length, stat.size - offset), offset);
      if (!bytesRead) break;
      offset += bytesRead;
      const chunk = decoder.decode(buffer.subarray(0, bytesRead), {stream: offset < stat.size});
      for (let i = 0; i < chunk.length; i++) {
        const char = chunk[i];
        if (char === '\0') throw new Error('Binary file; UTF-8 text required');
        if (char === '\n') {
          if (line >= start) {
            if (text.length + current.length + 1 > MAX_CHARS) {
              text += current; end = line; return report(true, true);
            }
            text += current.replace(/\r$/, '') + '\n'; current = ''; end = line;
            if (line - start + 1 >= limit) return report(i + 1 < chunk.length || offset < stat.size);
          }
          line++;
        } else if (line >= start) {
          end = line;
          if (text.length + current.length >= MAX_CHARS - 1) {
            text += current; return report(true, true);
          }
          current += char;
        }
      }
    }
    if (current) { text += current.replace(/\r$/, '') + '\n'; end = line; }
    return report(false);
  } finally { await handle.close(); }
}
module.exports = {readTextPage};
