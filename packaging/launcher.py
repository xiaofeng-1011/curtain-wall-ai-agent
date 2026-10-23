"""Standalone local launcher. All mutable data stays outside the installation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import traceback
import urllib.request
import webbrowser


def application_directory():
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS) / 'app'
    return Path(__file__).resolve().parents[1] / '幕墙方案助手' / '_internal' / 'app'


def configure():
    app = application_directory()
    workspace = Path(os.environ.get('MW_RUNTIME_DIR') or Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'MqHelper' / 'workspace').resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    os.environ['MW_RUNTIME_DIR'] = str(workspace)
    os.environ['STREAMLIT_BROWSER_GATHER_USAGE_STATS'] = 'false'
    os.environ['STREAMLIT_GLOBAL_DEVELOPMENT_MODE'] = 'false'
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(app))
    os.chdir(app)
    return app, workspace


def self_test(workspace, output):
    from storage.standard_library import StandardLibrary
    from storage.bundled_standards import initialize_builtin
    from evidence.standard_retrieval import StandardRetriever
    report = {'ok': False, 'frozen': bool(getattr(sys, 'frozen', False)), 'workspace': str(workspace)}
    library = StandardLibrary(workspace)
    retriever = None
    try:
        status = initialize_builtin(library)
        report['builtin'] = status
        if not status['ok']:
            raise RuntimeError(status['message'])
        retriever = StandardRetriever(library)
        result = retriever.search('JGJ 102 玻璃幕墙', [status['collection_id']], top_k=3)
        report['search'] = {'query': result['query'], 'hits': len(result['evidence']), 'stale': result['stale']}
        if result['stale'] or not result['evidence']:
            raise RuntimeError('内置规范检索未返回有效结果')
        hit = result['evidence'][0]
        page = hit['sources'][0]['page']
        png = library.page_png(hit['collection_id'], hit['doc_id'], page, dpi=72)
        image = workspace / 'self-test-original-page.png'
        image.write_bytes(png)
        report['original_page'] = {'doc_id': hit['doc_id'], 'page': page, 'png_bytes': len(png), 'path': str(image)}
        report['ok'] = png.startswith(b'\x89PNG\r\n\x1a\n')
    except Exception:
        report['error'] = traceback.format_exc()
    finally:
        if retriever:
            retriever.close()
        library.db.close()
    output = Path(output) if output else workspace / 'self-test.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if sys.stdout:
        print(str(output))
    return 0 if report['ok'] else 1


def health(port):
    try:
        # Ignore machine proxy configuration for the private loopback server.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{port}/_stcore/health', timeout=1) as reply:
            return reply.status == 200
    except Exception:
        return False


def serve(app, workspace, port):
    log = (workspace / 'launcher.log').open('a', encoding='utf-8', buffering=1)
    sys.stdout = log
    sys.stderr = log
    from streamlit.web import bootstrap
    options = {
        'server.address': '127.0.0.1', 'server.port': port,
        'server.headless': True, 'server.fileWatcherType': 'none',
        'browser.gatherUsageStats': False, 'global.developmentMode': False,
    }
    bootstrap.load_config_options(options)
    bootstrap.run(str(app / 'app.py'), False, [], options)


def launch(app, workspace):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    state = workspace / 'launcher-state.json'
    if os.environ.get('MW_NO_BROWSER') == '1':
        state.write_text(json.dumps({'pid': os.getpid(), 'port': port, 'url': url}), encoding='utf-8')
        serve(app, workspace, port)
        return 0
    import tkinter as tk
    args = [sys.executable]
    if not getattr(sys, 'frozen', False):
        args.append(str(Path(__file__).resolve()))
    args.extend(['--serve', '--port', str(port)])
    child = subprocess.Popen(args, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    state.write_text(json.dumps({'pid': child.pid, 'port': port, 'url': url}), encoding='utf-8')
    window = tk.Tk()
    window.title('幕墙方案助手')
    window.geometry('420x200')
    window.resizable(False, False)
    window.configure(bg='#EEF2F6')
    status = tk.StringVar(value='正在启动，请稍候…')
    tk.Label(window, text='幕墙方案助手', font=('Microsoft YaHei UI', 19, 'bold'), bg='#EEF2F6', fg='#172334').pack(pady=(20, 10))
    tk.Label(window, textvariable=status, font=('Microsoft YaHei UI', 10), bg='#EEF2F6', fg='#172334').pack()
    buttons = tk.Frame(window, bg='#EEF2F6')
    buttons.pack(pady=18)
    opened = False
    started = time.monotonic()
    def stop():
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        window.destroy()
    def poll():
        nonlocal opened
        if child.poll() is not None:
            status.set('服务已退出；请查看工作区 launcher.log')
            return
        if health(port):
            status.set('已启动；关闭此窗口将退出程序')
            if not opened:
                opened = True
                webbrowser.open(url)
        elif time.monotonic() - started > 90:
            status.set('启动时间较长，请查看工作区 launcher.log')
        window.after(1000, poll)
    tk.Button(buttons, text='打开界面', command=lambda: webbrowser.open(url), width=13).pack(side='left', padx=6)
    tk.Button(buttons, text='退出程序', command=stop, width=13).pack(side='left', padx=6)
    window.protocol('WM_DELETE_WINDOW', stop)
    window.after(300, poll)
    window.mainloop()
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--port', type=int, default=8501)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--self-test-output')
    args = parser.parse_args()
    app, workspace = configure()
    if args.self_test:
        return self_test(workspace, args.self_test_output)
    if args.serve:
        serve(app, workspace, args.port)
        return 0
    return launch(app, workspace)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        error = traceback.format_exc()
        root = Path(os.environ.get('MW_RUNTIME_DIR') or Path.home())
        root.mkdir(parents=True, exist_ok=True)
        (root / 'launcher-error.log').write_text(error, encoding='utf-8')
        if os.environ.get('MW_NO_BROWSER') != '1':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, '启动失败，请查看：' + str(root / 'launcher-error.log'), '幕墙方案助手', 16)
        sys.exit(1)
