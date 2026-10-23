# Windows 发布构建（V5）

V5 的修改基线是 `发布/幕墙方案助手_V4_UI修订_20261015`：业务代码来自其 `幕墙方案助手/_internal/app`，测试来自同目录的源码压缩包，内置规范资源（91 份登记文档的 PDF、数据库与索引）沿用该完整程序中的 `resources/standards`。冻结启动器和 Python 运行时也沿用该版本已验证的文件，本次没有重新执行 PyInstaller。

## 环境

使用 Python 3.13 x64，在本目录创建 `.venv`，安装 `requirements-build.txt`（精确版本见 `requirements-build-lock.txt`）。编译器使用官方 Inno Setup 6/7，把 ISCC.exe 路径传给 `-ISCC`，或安装到 `packaging/tools/InnoSetup`。Inno Setup 7.1.0 官方 x64 安装器 SHA-256：`0362a383ed217d4c4239b5933866dd96d3eb2102737da92f80f6057a4b40df2f`，下载源：`https://github.com/jrsoftware/issrc/releases/download/is-7_1_0/innosetup-7.1.0-x64.exe`。

## 构建

1. 新建发布目录，把 V4 完整程序目录 `幕墙方案助手/` 复制进去（含冻结启动器、运行时和规范资源）。
2. 运行：

```
powershell -ExecutionPolicy Bypass -File packaging/build.ps1 -SkipRuntime -Installer -ReleaseDir <发布目录> -ISCC <ISCC.exe> [-Python <python.exe>]
```

脚本同步业务代码与资源到 `<发布目录>/幕墙方案助手/_internal/app`（不复制用户工作区、密钥、测试或缓存），再生成 `幕墙方案助手_V5_安装包.exe`。安装包与 V4 使用同一 AppId，覆盖安装即升级。`build.ps1` 须保存为带 BOM 的 UTF-8，Windows PowerShell 5 才能正确读取中文。

3. `python packaging/finalize_v5.py --release <发布目录>`：逐一核对分发业务文件与源码一致、冻结运行时与 V4 一致、规范资源与资源清单一致，生成源码压缩包和新的《分发文件完整性清单.json》。

## 验收脚本

- `verify_install.py --release <发布目录> --target <临时目录>`：真实静默安装（`/PORTABLETEST=1` 不登记卸载、不建快捷方式），隔离用户数据，去掉 PATH 中的 Python，运行冻结程序离线自检和服务健康检查。
- `verify_v5_frozen.py --exe <幕墙方案助手.exe> --out <记录目录> --prefix <名称>`：Playwright 驱动冻结程序做端到端检查，使用本机已配置的模型（只读入子进程环境变量，不写入任何文件）。
- `verify_v5_upgrade.py --v4-installer … --v5-installer … --target … --out …`：先装 V4 并写入用户数据，再用 V5 覆盖安装，核对数据与配置保留。
- `verify_v5_online.py --app <业务代码目录> --out <记录目录> --label v5|v4`：同一组问题分别在 V5 与 V4 上用真实模型运行，生成修改前后样例的原始记录；`make_samples.py` 由记录生成样例文档。

正常启动会显示服务控制窗口并打开浏览器；关闭控制窗口即退出本地服务，只监听 127.0.0.1。用户数据默认 `%LOCALAPPDATA%/MqHelper/workspace`，模型设置在 `%LOCALAPPDATA%/MqHelper/config.toml`，安装、升级、卸载都不删除。验收时用 `MW_RUNTIME_DIR` 指定空白工作区，`MW_NO_BROWSER=1` 禁用浏览器和控制窗口。可选环境变量 `MW_ANALYSIS_TIMEOUT`（默认 120 秒）控制规范解读和方案分析的模型超时。
