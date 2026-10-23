# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import ast
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
datas = collect_data_files('streamlit')
for package in ('streamlit', 'openai', 'anthropic', 'pymupdf', 'pandas', 'openpyxl'):
    datas += copy_metadata(package)
hidden = ['fitz', 'pymupdf', 'openpyxl', 'openpyxl.styles', 'openai', 'anthropic', 'pandas', 'sqlite3', 'tkinter', 'streamlit.web.bootstrap']
hidden += collect_submodules('streamlit', filter=lambda name: '.testing' not in name)
app_source = root / '幕墙方案助手' / '_internal' / 'app'
local_modules = {p.stem for p in app_source.iterdir()}
for source in app_source.rglob('*.py'):
    if 'tests' in source.relative_to(app_source).parts:
        continue
    for node in ast.walk(ast.parse(source.read_text(encoding='utf-8-sig'))):
        names = [item.name for item in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and not node.level and node.module else []
        hidden.extend(name for name in names if name.split('.')[0] not in local_modules)
hidden = sorted(set(hidden))
a = Analysis([str(root / 'packaging' / 'launcher.py')], pathex=[], binaries=[], datas=datas,
             hiddenimports=hidden, hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['pytest', 'IPython', 'matplotlib', 'scipy', 'torch', 'tensorflow'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='幕墙方案助手', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='幕墙方案助手')
