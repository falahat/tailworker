"""Keep the shared machine tool small and source free of private material."""
from pathlib import Path
import ast
import subprocess

root = Path(__file__).resolve().parent
code = sum(len((root / name).read_text().splitlines()) for name in ('tailworker', 'bootstrap.sh', 'check.py'))
tests = sum(len(path.read_text().splitlines()) for path in (root / 'tests').rglob('*.py'))
assert code < 800, code
assert tests < 300, tests
for name in ('tailworker', 'check.py'):
    ast.parse((root / name).read_text())
subprocess.run(['bash', '-n', str(root / 'bootstrap.sh')], check=True)
for name in subprocess.check_output(['git', 'ls-files'], cwd=root, text=True).splitlines():
    path = root / name
    assert not path.is_symlink() and 'secrets' not in path.parts, name
    assert 'PRIVATE ' + 'KEY-----' not in path.read_text(), name
print(f'code_lines={code} test_lines={tests}')
