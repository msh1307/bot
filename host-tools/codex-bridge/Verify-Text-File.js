'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const {readTextPage} = require('./text-file');
(async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'evo-text-page-'));
  const file = path.join(root, 'report.txt');
  try {
    await fs.writeFile(file, '\uFEFF한글🙂\r\nsecond\r\nthird');
    let page = await readTextPage({path:file, limit:1});
    assert.equal(page.text, '한글🙂\n');assert.equal(page.next_line, 2);
    page = await readTextPage({path:file, start_line:2});
    assert.equal(page.text, 'second\nthird\n');assert.equal(page.next_line, null);
    assert.equal((await readTextPage({path:file, start_line:8})).text, '');
    await fs.writeFile(file, 'x'.repeat(20000));
    page = await readTextPage({path:file});
    assert(page.text.length <= 12000 && page.partial_line && page.next_line === null);
    await fs.writeFile(file, 'x'.repeat(11999) + '\n\n');
    assert((await readTextPage({path:file})).text.length <= 12000);
    await fs.writeFile(file, 'x'.repeat(65535) + '\n한글\n');
    assert.equal((await readTextPage({path:file,start_line:2})).text, '한글\n');
    for (const body of [Buffer.from([0,1,2]), Buffer.from([0xFF])]) {
      await fs.writeFile(file, body);await assert.rejects(readTextPage({path:file}));
    }
    await fs.truncate(file, 17*1024*1024);
    await assert.rejects(readTextPage({path:file}), /16 MiB/);
    await assert.rejects(readTextPage({path:root}), /Text file/);
    await assert.rejects(readTextPage({path:file, limit:201}));
    await assert.rejects(readTextPage({path:'relative.txt'}));
    console.log('PASS UTF-8/CRLF pages, EOF, byte/character limits, binary/directory/invalid input rejection');
  } finally {
    await fs.rm(file, {force:true});await fs.rmdir(root);
  }
})().catch(error => {console.error(error);process.exitCode=1;});
