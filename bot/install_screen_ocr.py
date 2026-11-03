"""Root, Debian/Ubuntu: private OCR runtime; no apt install or service restarts."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path('/opt/evo-bot/screen-ocr')
PACKAGES = ['tesseract-ocr', 'tesseract-ocr-eng', 'libtesseract5', 'libleptonica6',
            'libjpeg-turbo8', 'libgif7', 'libtiff6', 'libwebp7', 'libwebpmux3',
            'libopenjp2-7', 'libdeflate0', 'libjbig0', 'liblerc4']


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run as root')
    ROOT.mkdir(parents=True, exist_ok=True)
    manifest = []
    with tempfile.TemporaryDirectory(prefix='evo-screen-ocr-', dir='/run') as tmp:
        subprocess.run(['apt-get', 'download', *PACKAGES], cwd=tmp, check=True)
        for package in sorted(Path(tmp).glob('*.deb')):
            subprocess.run(['dpkg-deb', '-x', str(package), str(ROOT)], check=True)
            manifest.append({'package': package.name, 'sha256': hashlib.sha256(package.read_bytes()).hexdigest()})
    env = {'PATH': '/usr/bin:/bin', 'LD_LIBRARY_PATH': str(ROOT/'usr/lib/x86_64-linux-gnu')}
    subprocess.run([str(ROOT/'usr/bin/tesseract'), '--version'], env=env, check=True)
    (ROOT/'packages.json').write_text(json.dumps(manifest, indent=2)+'\n')


if __name__ == '__main__':
    main()
