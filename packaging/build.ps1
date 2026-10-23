param([switch]$SkipRuntime, [switch]$Installer, [string]$ISCC, [string]$ReleaseDir, [string]$Python)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$release = if ($ReleaseDir) { [IO.Path]::GetFullPath($ReleaseDir) } else { Join-Path $root '发布/幕墙方案助手_V5_智能解读' }
if (-not $Python) { $Python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe' }
if (-not (Test-Path -LiteralPath $Python)) { throw '请先根据 packaging/README.md 创建构建环境，或用 -Python 指定 Python 3.13 解释器。' }
if (-not $SkipRuntime) {
    & $Python -m PyInstaller --noconfirm --distpath $release --workpath (Join-Path $PSScriptRoot 'build') (Join-Path $PSScriptRoot 'MqHelper.spec')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 构建失败。' }
}
& $Python (Join-Path $PSScriptRoot 'sync_release.py') --destination $release
if ($LASTEXITCODE -ne 0) { throw '程序资源同步失败。' }
if ($Installer) {
    if (-not $ISCC) { $ISCC = Join-Path $PSScriptRoot 'tools/InnoSetup/ISCC.exe' }
    & $ISCC ('/DReleaseDir=' + $release) (Join-Path $PSScriptRoot 'installer.iss')
    if ($LASTEXITCODE -ne 0) { throw '安装包构建失败。' }
}
