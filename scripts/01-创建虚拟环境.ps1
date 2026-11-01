<#
.SYNOPSIS
    创建本项目专用的 Python 虚拟环境并安装依赖（幂等，可重复执行）。

.DESCRIPTION
    《操作系统》课程实训项目 —— 面向智慧地质勘查的无人机集群并发自适应负载均衡调度仿真系统

    脚本步骤：
      1. 定位可用的 Python 解释器（优先 3.11，本项目实测基线）；
      2. 在项目根创建 .venv（已存在则复用，不删除）；
      3. 升级 pip（走清华镜像，避免超时）；
      4. 安装 requirements.txt（PyYAML / matplotlib / pandas / PyMySQL）；
      5. 自检：打印解释器路径、版本、关键依赖版本；
      6. 提示后续步骤（建库 -> 标定 -> 跑实验 -> 出图）。

    为什么用 .venv 而不是全局环境：
      本机系统 PATH 里的 python 是 3.11，但全局 site-packages 缺少 matplotlib，
      且不同课程项目依赖版本容易冲突。虚拟环境把依赖锁在项目内，删掉 .venv 即可完全重置。

.PARAMETER PythonPath
    显式指定 Python 解释器路径。不传则自动在下列位置中挑选第一个存在的：
      C:\Users\<用户>\AppData\Local\Programs\Python\Python311\python.exe
      C:\Users\<用户>\AppData\Local\Programs\Python\Python312\python.exe
      PATH 里的 python

.PARAMETER Force
    删掉已有 .venv 后重建（依赖装坏时用）。

.EXAMPLE
    .\scripts\01-创建虚拟环境.ps1
    .\scripts\01-创建虚拟环境.ps1 -Force

.NOTES
    执行策略报错时先运行：Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$PythonPath = '',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root '.venv'
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
$ReqFile = Join-Path $Root 'requirements.txt'
$Mirror = 'https://pypi.tuna.tsinghua.edu.cn/simple'

function Write-Step { param([string]$Text)
    Write-Host ''
    Write-Host ('=' * 74) -ForegroundColor DarkCyan
    Write-Host ("  " + $Text) -ForegroundColor Cyan
    Write-Host ('=' * 74) -ForegroundColor DarkCyan
}
function Fail { param([string]$Text)
    Write-Host ''
    Write-Host ("  ✗ " + $Text) -ForegroundColor Red
    exit 1
}

Write-Step '1 / 5   定位 Python 解释器'
if (-not $PythonPath) {
    $candidates = @(
        (Join-Path $env:USERPROFILE 'AppData\Local\Programs\Python\Python311\python.exe'),
        (Join-Path $env:USERPROFILE 'AppData\Local\Programs\Python\Python312\python.exe'),
        (Join-Path $env:USERPROFILE 'AppData\Local\Programs\Python\Python310\python.exe')
    )
    $PythonPath = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $PythonPath) {
        $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($cmd) { $PythonPath = $cmd.Source }
    }
}
if (-not $PythonPath -or -not (Test-Path $PythonPath)) {
    Fail "找不到 Python 解释器。请安装 Python 3.11 后重试，或用 -PythonPath 指定。`n        下载：https://www.python.org/downloads/ （安装时勾选 Add python.exe to PATH）"
}
$pyVer = & $PythonPath -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
Write-Host ("  解释器 : " + $PythonPath)
Write-Host ("  版本   : " + $pyVer) -ForegroundColor Green
if ([version]($pyVer.Split('-')[0]) -lt [version]'3.8') {
    Fail "Python 版本过低（$pyVer），本项目要求 3.8+，推荐 3.11。"
}

Write-Step '2 / 5   创建虚拟环境 .venv'
if ($Force -and (Test-Path $Venv)) {
    Write-Host '  -Force：删除已有 .venv ...' -ForegroundColor Yellow
    Remove-Item -Recurse -Force $Venv
}
if (Test-Path $VenvPy) {
    Write-Host ("  ✓ 已存在，复用：" + $Venv) -ForegroundColor Green
} else {
    Write-Host ("  正在创建：" + $Venv)
    & $PythonPath -m venv $Venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPy)) { Fail '创建虚拟环境失败' }
    Write-Host '  ✓ 创建完成' -ForegroundColor Green
}

Write-Step '3 / 5   升级 pip（清华镜像）'
& $VenvPy -m pip install --upgrade pip -i $Mirror --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host '  [警告] pip 升级失败，继续使用现有版本' -ForegroundColor Yellow
} else {
    Write-Host '  ✓ pip 已是最新' -ForegroundColor Green
}

Write-Step '4 / 5   安装项目依赖'
if (-not (Test-Path $ReqFile)) { Fail ("requirements.txt 不存在：" + $ReqFile) }
Write-Host ("  依赖清单：" + $ReqFile)
& $VenvPy -m pip install -r $ReqFile -i $Mirror
if ($LASTEXITCODE -ne 0) {
    Fail "依赖安装失败。常见原因：网络超时（确认已在用清华镜像）、缺少编译器（本项目全为纯 Python 包，一般不会）。"
}
Write-Host '  ✓ 依赖安装完成' -ForegroundColor Green

Write-Step '5 / 5   自检'
& $VenvPy -c @"
import sys
print(f'  解释器   : {sys.executable}')
print(f'  版本     : {sys.version.split()[0]}')
for mod, name in [('yaml','PyYAML'), ('matplotlib','matplotlib'),
                  ('pandas','pandas'), ('pymysql','PyMySQL'), ('numpy','numpy')]:
    try:
        m = __import__(mod)
        print(f'  {name:<12}: {getattr(m, \"__version__\", \"?\")}')
    except Exception as e:
        print(f'  {name:<12}: 缺失 ({e})')
import threading, queue, concurrent.futures
print('  标准库    : threading / queue / concurrent.futures 可用（本项目核心）')
"@

Write-Host ''
Write-Host '  ✓ 环境就绪。接下来按顺序执行：' -ForegroundColor Green
Write-Host ''
Write-Host '     # 1) 环境体检（依赖 / MySQL / 中文字体 / 模块完整性）'
Write-Host '     .\.venv\Scripts\python.exe run.py check'
Write-Host ''
Write-Host '     # 2) 初始化数据库（幂等；没启动 MySQL 可跳过，实验会降级只写 CSV）'
Write-Host '     .\scripts\02-初始化数据库.ps1'
Write-Host ''
Write-Host '     # 3) 三工况容量标定（报告"参数设计依据"章节的数据来源）'
Write-Host '     .\.venv\Scripts\python.exe run.py calibrate'
Write-Host ''
Write-Host '     # 4) 全量对照实验（3 工况 × 5 策略 = 15 组，约 100 秒）'
Write-Host '     .\.venv\Scripts\python.exe run.py all'
Write-Host ''
Write-Host '     # 5) 参数敏感性实验（验证"静态参数固化"痛点，创新拓展项）'
Write-Host '     .\.venv\Scripts\python.exe run.py sensitivity'
Write-Host ''
Write-Host '  一键跑完 2~4 步： .\scripts\03-一键跑全量实验.ps1' -ForegroundColor Gray
Write-Host ''
exit 0
