"""Copy only distributable application files; user data and credentials excluded."""
from pathlib import Path
import shutil
import argparse

root = Path(__file__).resolve().parents[1]
source = root / '幕墙方案助手' / '_internal' / 'app'
parser = argparse.ArgumentParser()
parser.add_argument('--destination', type=Path, default=root / '发布' / '幕墙方案助手_V5_智能解读')
args = parser.parse_args()
destination = args.destination.resolve() / '幕墙方案助手' / '_internal' / 'app'
if destination == source.resolve() or source.resolve().is_relative_to(destination) or destination.is_relative_to(source.resolve()):
    raise ValueError('发布目录必须与源码目录分离')
excluded = {'__pycache__', '.pytest_cache', '.git', 'workspace', 'runtime', 'tests', 'secrets.toml', '.env'}
count = 0
included = set()
for path in source.rglob('*'):
    relative = path.relative_to(source)
    if any(part in excluded for part in relative.parts) or path.suffix in {'.pyc', '.pyo'}:
        continue
    if path.is_file():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        included.add(relative)
        count += 1
for target in destination.rglob('*'):
    if target.is_file() and target.relative_to(destination) not in included:
        if not target.resolve().is_relative_to(destination.resolve()):
            raise RuntimeError('拒绝删除发布目录以外的路径')
        target.unlink()
print(f'Copied {count} application and bundled resource files to {destination}')
