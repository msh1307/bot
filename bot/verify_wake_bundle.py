"""Offline regressions, using the deployed Pi and Python runtime; no Discord/model calls."""
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys

root = Path(__file__).resolve().parent
output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
env = dict(os.environ, PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE='1')
report = {'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'suites': {}, 'passed': 0}
tests = ['verify_codex_wake.py', 'verify_task_queue.py', 'verify_codex_live_fixes.py',
         'verify_codex_existing.py', 'verify_codex_questions.py', 'verify_codex_cancellation.py',
         'verify_gateway.py', 'verify_schedule.py', 'verify_recovery.py']
for name in tests:
    run = subprocess.run([sys.executable, str(root / name)], env=env, text=True,
                         capture_output=True, timeout=90)
    text = run.stdout + run.stderr
    (output / (name + '.txt')).write_text(text)
    count = re.search(r'Ran (\d+) tests?', text)
    result = {'exit_code': run.returncode, 'tests': int(count[1]) if count else None}
    report['suites'][name] = result
    print(name, json.dumps(result), flush=True)
    if run.returncode:
        print(text[-9000:], flush=True)
        (output / 'tests.json').write_text(json.dumps(report, indent=2))
        raise SystemExit(1)
    report['passed'] += result['tests'] or 0
for name, argv in [
    ('pi_compaction', [sys.executable, str(root / 'verify_pi_compaction.py'),
                       str(root / 'pi-runner.mjs'), str(output / 'pi-compaction.json')]),
    ('pi_compaction_overflow', [sys.executable, str(root / 'verify_pi_compaction.py'),
                       str(root / 'pi-runner.mjs'), str(output / 'pi-compaction-overflow.json'), '--overflow']),
    ('pi_overflow', ['node', str(root / 'verify_pi_overflow.mjs')]),
]:
    run = subprocess.run(argv, env=env, text=True, capture_output=True, timeout=90)
    (output / (name + '.txt')).write_text(run.stdout + run.stderr)
    result = {'exit_code': run.returncode}
    if not run.returncode:
        data = json.loads(run.stdout.strip().splitlines()[-1])
        result.update(data)
        report['passed'] += data.get('passed', 0)
    report['suites'][name] = result
    print(name, json.dumps(result), flush=True)
    if run.returncode:
        print((run.stdout + run.stderr)[-9000:], flush=True)
        (output / 'tests.json').write_text(json.dumps(report, indent=2))
        raise SystemExit(1)
(output / 'tests.json').write_text(json.dumps(report, indent=2))
print(json.dumps({'passed': report['passed'], 'failed': 0}), flush=True)
