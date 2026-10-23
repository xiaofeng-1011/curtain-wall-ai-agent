"""Isolated, real installer and frozen executable acceptance on Windows."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess
import time
import urllib.request
import winreg

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / '发布' / '幕墙方案助手_V5_智能解读'


def registration():
    key_name = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{782B3188-20BC-4E52-85F2-731EB0180624}_is1'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
            return [winreg.EnumValue(key, index) for index in range(winreg.QueryInfoKey(key)[1])]
    except FileNotFoundError:
        return None


def verify_ui(url):
    from websockets.sync.client import connect
    from streamlit.proto.BackMsg_pb2 import BackMsg
    from streamlit.proto.ForwardMsg_pb2 import ForwardMsg
    request = BackMsg()
    request.rerun_script.query_string = ''
    request.rerun_script.page_script_hash = ''
    errors = []
    count = 0
    with connect(url.replace('http:', 'ws:') + '/_stcore/stream', subprotocols=['streamlit'],
                 origin=url, proxy=None, max_size=16_000_000) as ws:
        ws.send(request.SerializeToString())
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            message = ForwardMsg()
            message.ParseFromString(ws.recv(timeout=180))
            count += 1
            kind = message.WhichOneof('type')
            if kind == 'delta' and message.delta.HasField('new_element') and message.delta.new_element.HasField('exception'):
                errors.append(str(message.delta.new_element.exception))
            if kind == 'script_finished':
                return {'messages': count, 'script_finished': message.script_finished, 'errors': errors}
    raise RuntimeError('界面执行超时')


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', default=str(Path(os.environ['TEMP']) / ('幕墙 安装验收_' + time.strftime('%Y%m%d_%H%M%S'))))
    parser.add_argument('--release', type=Path, default=RELEASE)
    parser.add_argument('--installer-name', default='幕墙方案助手_V5_安装包.exe')
    args = parser.parse_args()
    target = Path(args.target)
    target.mkdir(parents=True, exist_ok=True)
    if (target / '程序').exists():
        raise RuntimeError('使用新的独立验收目录，避免覆盖已有内容')
    before = registration()
    installer = args.release / args.installer_name
    env = dict(os.environ, MW_RUNTIME_DIR=str(target / '用户 工作区'), LOCALAPPDATA=str(target / '隔离用户'), MW_NO_BROWSER='1')
    # Strip Python installations from PATH to prove the executable supplies its own runtime.
    env['PATH'] = os.pathsep.join([str(Path(os.environ['SystemRoot']) / 'System32'), os.environ['SystemRoot']])
    install = subprocess.run([str(installer), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/PORTABLETEST=1',
                              '/TASKS=', f'/DIR={target / "程序"}', f'/LOG={target / "install.log"}'], env=env, timeout=600)
    if install.returncode:
        raise RuntimeError(f'安装失败: {install.returncode}')
    if registration() != before:
        raise RuntimeError('验收安装改变了用户卸载登记')
    if list((target / '程序').glob('unins*')):
        raise RuntimeError('验收模式意外创建了卸载程序')
    exe = target / '程序' / '幕墙方案助手.exe'
    result = subprocess.run([str(exe), '--self-test', '--self-test-output', str(target / 'self-test.json')], env=env, timeout=240)
    report = json.loads((target / 'self-test.json').read_text(encoding='utf-8'))
    report['installer_exit_code'] = install.returncode
    report['self_test_exit_code'] = result.returncode
    report['registry_unchanged'] = registration() == before
    report['install_directory'] = str(target / '程序')
    with installer.open('rb') as stream:
        report['installer_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
    child = subprocess.Popen([str(exe)], env=env)
    try:
        deadline = time.monotonic() + 90
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError('服务启动失败；查看隔离工作区launcher.log')
            try:
                state = json.loads((Path(env['MW_RUNTIME_DIR']) / 'launcher-state.json').read_text())
                with opener.open(state['url'] + '/_stcore/health', timeout=2) as response:
                    if response.status == 200:
                        report['health_url'] = state['url'] + '/_stcore/health'
                        report['health_status'] = response.status
                        break
            except (OSError, ValueError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError('服务健康检查超时')
        report['ui'] = verify_ui(state['url'])
        report['ok'] = report['ok'] and result.returncode == 0 and report['registry_unchanged'] and not report['ui']['errors'] and report['ui']['script_finished'] == 0
    finally:
        child.terminate()
        child.wait(timeout=15)
    (target / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(run())
