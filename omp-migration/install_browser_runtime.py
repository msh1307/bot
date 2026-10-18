"""Install the official, pinned Linux x64 Shell without changing the bot service."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

VERSION = '155.0.8059.84'
ROOT = Path('/opt/evo-omp/browser-runtime')
URL = f'https://storage.googleapis.com/chrome-for-testing-public/{VERSION}/linux64/chrome-headless-shell-linux64.zip'


def main():
    Path('/opt/evo-omp/sandbox-etc/fonts').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='evo-shell-install-', dir='/run') as tmp:
        archive = Path(tmp) / 'shell.zip'
        with urllib.request.urlopen(URL, timeout=90) as response, archive.open('wb') as out:
            shutil.copyfileobj(response, out)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        with zipfile.ZipFile(archive) as source:
            assert all(not Path(n).is_absolute() and '..' not in Path(n).parts for n in source.namelist())
            source.extractall(Path(tmp) / 'extracted')
        source = Path(tmp) / 'extracted/chrome-headless-shell-linux64'
        ROOT.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copytree(source, ROOT / 'chrome-headless-shell-linux64', dirs_exist_ok=True)
        binary = ROOT / 'chrome-headless-shell-linux64/chrome-headless-shell'
        binary.chmod(0o755)
        for name in ('chrome_crashpad_handler', 'chrome_sandbox'):
            p = binary.parent / name
            if p.exists(): p.chmod(0o755)
        actual = subprocess.check_output([str(binary), '--version'], text=True).strip()
        assert VERSION in actual, actual
        metadata = {'url': URL, 'version': VERSION, 'reported_version': actual,
                    'download_sha256': digest, 'sha256_is_local_download_digest': True,
                    'executable': str(binary)}
        (ROOT / 'installation.json').write_text(json.dumps(metadata, indent=2) + '\n')
        print(json.dumps(metadata), flush=True)
    subprocess.run(['/opt/evo-omp/venv/bin/python', '-m', 'pip', 'install',
                    '--disable-pip-version-check', 'playwright==1.62.0'], check=True)


if __name__ == '__main__':
    main()
