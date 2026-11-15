"""Fail CI if provider secrets or private files reach the public frontend build."""
from pathlib import Path
import os
import re

root = Path('dist')
patterns = [re.compile(rb'sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}'), re.compile(rb'AIza[A-Za-z0-9_-]{30,}')]
secrets = [v.encode() for k, v in os.environ.items() if len(v) >= 16 and any(s in k for s in ('API_KEY', 'EARTHDATA_TOKEN', 'DRONE_TOKEN'))]
unsafe = []
for path in root.rglob('*'):
    if not path.is_file():
        continue
    if path.name.startswith('.env') or path.suffix in {'.pem', '.key'}:
        unsafe.append(str(path))
    if path.suffix in {'.js', '.css', '.html', '.json', '.map'}:
        content = path.read_bytes()
        if any(p.search(content) for p in patterns) or any(s in content for s in secrets):
            unsafe.append(str(path))
if unsafe:
    raise SystemExit('Private configuration detected in: ' + ', '.join(sorted(set(unsafe))))
print('Public build contains no detected provider credentials or private configuration files.')
