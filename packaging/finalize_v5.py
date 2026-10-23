"""整理 V5 发布：核对分发文件确实是本次源码、核对内置规范资源、打包源码、生成新的完整性清单。

python packaging/finalize_v5.py --release <发布目录>
不修改 V4 发布目录和旧清单。
"""
import argparse
import hashlib
import json
import shutil
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
source = root / '幕墙方案助手' / '_internal' / 'app'
ap = argparse.ArgumentParser()
ap.add_argument('--release', type=Path, required=True)
ap.add_argument('--v4-release', type=Path, default=root.parent / '发布' / '幕墙方案助手_V4_UI修订_20261015')
args = ap.parse_args()
release = args.release.resolve()
target = release / '幕墙方案助手' / '_internal' / 'app'
excluded = {'__pycache__', '.pytest_cache', '.git', 'workspace', 'runtime', 'tests', 'secrets.toml', '.env', '.claude'}


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def app_files(base):
    for path in base.rglob('*'):
        rel = path.relative_to(base)
        if path.is_file() and not any(p in excluded for p in rel.parts) and path.suffix not in ('.pyc', '.pyo'):
            yield rel


# 1. 分发业务文件与本次源码逐一一致
mismatch = [str(r) for r in app_files(source) if not (target / r).is_file() or digest(source / r) != digest(target / r)]
extra = [str(r) for r in app_files(target) if not (source / r).is_file()]
assert not mismatch and not extra, (mismatch[:5], extra[:5])
verified = sum(1 for _ in app_files(source))
v5_modules = ['agent/claim_check.py', 'agent/change_intent.py', 'evidence/change_check.py', 'evidence/standard_meta.py',
              'reporting/analysis.py']
assert all((target / m).is_file() for m in v5_modules)
old_app = args.v4_release / '幕墙方案助手' / '_internal' / 'app'
changed = sorted(str(r).replace('\\', '/') for r in app_files(source)
                 if 'resources' not in r.parts and (not (old_app / r).is_file() or digest(old_app / r) != digest(source / r)))

# 2. 冻结启动器与运行时沿用 V4（逐一核对非业务文件）
runtime_same = []
for path in (args.v4_release / '幕墙方案助手').rglob('*'):
    rel = path.relative_to(args.v4_release / '幕墙方案助手')
    if path.is_file() and not str(rel).replace('\\', '/').startswith('_internal/app/'):
        assert digest(path) == digest(release / '幕墙方案助手' / rel), str(rel)
        runtime_same.append(rel)

# 3. 内置规范资源：按资源包自身清单逐文件核对
bundle = target / 'resources' / 'standards'
manifest = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
bad = [f['path'] for f in manifest['files'] if not (bundle / f['path']).is_file()
       or (bundle / f['path']).stat().st_size != f['size'] or digest(bundle / f['path']) != f['sha256']]
pdfs = [f for f in manifest['files'] if f['path'].lower().endswith('.pdf')]
resources = {'bundle_manifest_files': len(manifest['files']), 'pdf_files': len(pdfs), 'mismatch': bad,
             'counts': manifest.get('counts'), 'index_version': manifest.get('index_version'),
             # 每个文件都已按清单核对 SHA-256；清单本身与 V4 相同即说明资源与 V4 一致。
             'manifest_identical_to_v4': digest(bundle / 'manifest.json') == digest(old_app / 'resources' / 'standards' / 'manifest.json')}
assert not bad
records = release / '验收记录'
records.mkdir(exist_ok=True)
(records / '资源完整性核对.json').write_text(json.dumps(resources, ensure_ascii=False, indent=2), encoding='utf-8')
(records / '分发文件核对.json').write_text(json.dumps({
    'application_files_identical_to_source': verified, 'v5_modules_present': v5_modules,
    'changed_or_new_vs_v4': changed, 'frozen_runtime_files_identical_to_v4': len(runtime_same),
    'note': '冻结启动器与 Python 运行时沿用 V4 已验证版本（未重新执行 PyInstaller），业务代码由构建脚本同步。'},
    ensure_ascii=False, indent=2), encoding='utf-8')

# 4. 源码与构建脚本（含测试；不重复包含规范 PDF 和运行时）
archive = release / '幕墙方案助手_V5_源码与构建脚本.zip'
with ZipFile(archive, 'w', ZIP_DEFLATED) as z:
    for path in source.rglob('*'):
        rel = path.relative_to(source)
        if not path.is_file() or any(p in (excluded - {'tests'}) | {'resources'} for p in rel.parts) or path.suffix in ('.pyc', '.pyo'):
            continue
        z.write(path, Path('幕墙方案助手/_internal/app') / rel)
    z.write(bundle / 'manifest.json', '幕墙方案助手/_internal/app/resources/standards/manifest.json')
    for path in (root / 'packaging').iterdir():
        if path.is_file() and path.suffix in ('.py', '.ps1', '.iss', '.spec', '.md', '.txt'):
            z.write(path, Path('packaging') / path.name)

# 5. 分发完整性清单：从本目录实际文件生成
items = []
for path in sorted(release.rglob('*')):
    if path.is_file() and path.name != '分发文件完整性清单.json':
        items.append({'path': path.relative_to(release).as_posix(), 'size': path.stat().st_size, 'sha256': digest(path)})
(release / '分发文件完整性清单.json').write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'verified_application_files': verified, 'changed_vs_v4': len(changed), 'runtime_identical': len(runtime_same),
                  'resource_files': len(manifest['files']), 'distribution_files': len(items),
                  'installer_sha256': digest(release / '幕墙方案助手_V5_安装包.exe')}, ensure_ascii=False, indent=2))
