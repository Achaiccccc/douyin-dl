# 一键启动本机测试服务（Windows PowerShell）
# 用法：
#   cd douyin-dl
#   .\start-local.ps1
# 首次会创建 .venv 并安装依赖；之后直接启动。需要重装依赖时加 -Update
# 也可双击 start-local.bat
param(
    [switch]$Update
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "未找到 python，请先安装 Python 3.12+ 并加入 PATH。"
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$venvUvicorn = Join-Path $PSScriptRoot ".venv\Scripts\uvicorn.exe"
$pipIndex = "https://mirrors.aliyun.com/pypi/simple/"
$pipExtra = "https://pypi.org/simple"

if (-not (Test-Path $venvPython)) {
    Write-Host "正在创建虚拟环境 .venv ..."
    python -m venv .venv
    $Update = $true
}

if ($Update -or -not (Test-Path $venvUvicorn)) {
    Write-Host "正在安装 / 更新依赖 ..."
    & $venvPython -m pip install -r (Join-Path $PSScriptRoot "requirements.txt") `
        -i $pipIndex --extra-index-url $pipExtra
}

if (-not $env:APP_PASSWORD) {
    $env:APP_PASSWORD = "123456"
}
$port = if ($env:PORT) { $env:PORT } else { "8080" }
$hostAddr = "127.0.0.1"

Write-Host ""
Write-Host "本地服务已准备就绪："
Write-Host "  地址: http://${hostAddr}:${port}/"
Write-Host "  密码: $env:APP_PASSWORD"
Write-Host "  健康检查: http://${hostAddr}:${port}/healthz"
Write-Host "按 Ctrl+C 停止。"
Write-Host ""

& $venvUvicorn app.main:app --host $hostAddr --port $port --reload