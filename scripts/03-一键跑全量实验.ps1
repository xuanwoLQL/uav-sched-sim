<#
.SYNOPSIS
    一键跑全量对照实验（3 工况 × 5 调度策略 = 15 组），并落库、出图、导出汇总表。

.DESCRIPTION
    《操作系统》课程实训项目 —— 面向智慧地质勘查的无人机集群并发自适应负载均衡调度仿真系统
    本脚本按以下顺序执行，任一步失败即停止并提示：

      1. 环境自检            python run.py check
      2. 三工况容量标定      python run.py calibrate
      3. 初始化 MySQL 库表   scripts\02-初始化数据库.ps1（幂等）
      4. 全量对照实验        python run.py all
      5. 汇总产出            results\summary.csv / charts\*.png

.PARAMETER NoDb
    不写数据库，只落本地 CSV/JSON（MySQL 未启动时用）。

.PARAMETER NoCalibrate
    跳过标定步骤（标定只读，很快，一般不需要跳过）。

.EXAMPLE
    .\scripts\03-一键跑全量实验.ps1
    .\scripts\03-一键跑全量实验.ps1 -NoDb

.NOTES
    执行策略若报错，先运行：Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$NoDb,
    [switch]$NoCalibrate
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

$Root  = Split-Path -Parent $PSScriptRoot
$Venv  = Join-Path $Root '.venv\Scripts\python.exe'
$Mysql = 'D:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe'

function Write-Step {
    param([string]$Text)
    Write-Host ''
    Write-Host ('=' * 74) -ForegroundColor DarkCyan
    Write-Host ("  " + $Text) -ForegroundColor Cyan
    Write-Host ('=' * 74) -ForegroundColor DarkCyan
}

function Fail {
    param([string]$Text)
    Write-Host ''
    Write-Host ("  ✗ " + $Text) -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
# 0. 前置检查
# ---------------------------------------------------------------------------
Write-Step '0 / 5   前置检查'
Write-Host ("  项目根目录 : " + $Root)
Write-Host ("  解释器     : " + $Venv)
Write-Host ("  数据库     : " + $(if ($NoDb) { '不启用（-NoDb）' } else { 'MySQL 8.0 @ 127.0.0.1:3306' }))

if (-not (Test-Path $Venv)) {
    Fail "找不到虚拟环境 $Venv`n        请先执行： .\scripts\01-创建虚拟环境.ps1"
}
$pyVer = & $Venv -V 2>&1
Write-Host ("  Python     : " + $pyVer) -ForegroundColor Green

Set-Location $Root

# ---------------------------------------------------------------------------
# 1. 环境自检
# ---------------------------------------------------------------------------
Write-Step '1 / 5   环境自检（依赖 / 数据库 / 中文字体 / 模块完整性）'
& $Venv run.py check
if ($LASTEXITCODE -ne 0) {
    Fail '环境自检未通过。请按上面 [PROBLEM] 提示修复后重跑。如果只是 MySQL 不可用，可加 -NoDb 参数。'
}

# ---------------------------------------------------------------------------
# 2. 三工况容量标定
# ---------------------------------------------------------------------------
if (-not $NoCalibrate) {
    Write-Step '2 / 5   三工况容量标定（提供负载 / 系统容量 / 负载率）'
    & $Venv run.py calibrate
    if ($LASTEXITCODE -ne 0) {
        Write-Host ''
        Write-Host '  [注意] 有工况偏离标定目标，但仍可继续跑实验；建议先调整 scenarios.py 的 survey_gap_k。' -ForegroundColor Yellow
    }
} else {
    Write-Host ''
    Write-Host '  已跳过标定（-NoCalibrate）' -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# 3. 初始化 MySQL
# ---------------------------------------------------------------------------
if (-not $NoDb) {
    Write-Step '3 / 5   初始化 MySQL 库表（幂等）'
    $initScript = Join-Path $PSScriptRoot '02-初始化数据库.ps1'
    if (Test-Path $initScript) {
        & $initScript
        if ($LASTEXITCODE -ne 0) {
            Write-Host ''
            Write-Host '  [注意] 数据库初始化失败，实验将继续但结果只落本地文件。' -ForegroundColor Yellow
        }
    } else {
        Write-Host ('  未找到 ' + $initScript + '，交由 run.py 在首次入库时自动建表。') -ForegroundColor Yellow
    }
} else {
    Write-Host ''
    Write-Host '  已跳过数据库初始化（-NoDb）' -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# 4. 全量对照实验
# ---------------------------------------------------------------------------
Write-Step '4 / 5   全量对照实验（3 工况 × 5 策略 = 15 组）'
$t0 = Get-Date
if ($NoDb) {
    & $Venv run.py all --no-db
} else {
    & $Venv run.py all
}
$code = $LASTEXITCODE
$elapsed = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
Write-Host ''
Write-Host ("  实验耗时 " + $elapsed + " 秒，退出码 " + $code) -ForegroundColor $(if ($code -eq 0) { 'Green' } else { 'Red' })

# ---------------------------------------------------------------------------
# 5. 汇总产出核对
# ---------------------------------------------------------------------------
Write-Step '5 / 5   产出核对'
$summary = Join-Path $Root 'results\summary.csv'
$charts  = Join-Path $Root 'charts'

if (Test-Path $summary) {
    $lines = (Get-Content $summary -Encoding UTF8).Count - 1
    Write-Host ("  ✓ results\summary.csv       " + $lines + " 行数据（期望 15）") -ForegroundColor Green
} else {
    Write-Host '  ✗ 未生成 results\summary.csv' -ForegroundColor Red
}

if (Test-Path $charts) {
    $pngs = @(Get-ChildItem $charts -Filter '*.png')
    Write-Host ("  ✓ charts\                    " + $pngs.Count + " 张图") -ForegroundColor Green
    foreach ($f in $pngs) { Write-Host ("      " + $f.Name) }
} else {
    Write-Host '  ✗ 未生成 charts 目录' -ForegroundColor Red
}

$raw = Join-Path $Root 'results\raw'
if (Test-Path $raw) {
    $n = @(Get-ChildItem $raw -Filter '*.json').Count
    Write-Host ("  ✓ results\raw\               " + $n + " 份 JSON") -ForegroundColor Green
}

if (-not $NoDb -and (Test-Path $Mysql)) {
    Write-Host ''
    Write-Host '  MySQL 中的结果核对：' -ForegroundColor Cyan
    # 密码不写死：优先环境变量 UAV_DB_PASSWORD，其次 config.yaml
    $dbPwd = $env:UAV_DB_PASSWORD
    if (-not $dbPwd) {
        $cfgLine = Select-String -Path (Join-Path $Root 'config.yaml') -Pattern '^\s*password:\s*(.+)$' | Select-Object -First 1
        if ($cfgLine) { $dbPwd = $cfgLine.Matches[0].Groups[1].Value.Trim().Trim('"').Trim("'") }
    }
    if ($dbPwd) {
        $env:MYSQL_PWD = $dbPwd
        & $Mysql --host=127.0.0.1 --user=root --database=uav_sched_sim --silent `
            -e "SELECT CONCAT('experiment 表 ', COUNT(*), ' 行') FROM experiment; SELECT CONCAT('exp_metric 表 ', COUNT(*), ' 行') FROM exp_metric; SELECT CONCAT('task_record 表 ', COUNT(*), ' 行') FROM task_record;"
        Remove-Item Env:\MYSQL_PWD -ErrorAction SilentlyContinue
    } else {
        Write-Host '  [跳过] 未取到数据库密码（设 UAV_DB_PASSWORD 或检查 config.yaml）' -ForegroundColor Yellow
    }
}

Write-Host ''
Write-Host '  全部完成。看结果：' -ForegroundColor Green
Write-Host ('    start "" "' + $charts + '"') -ForegroundColor Gray
Write-Host ('    code "' + $summary + '"')  -ForegroundColor Gray
Write-Host '    IDEA 2024.1.3 → Database 面板直连 MySQL 查 v_exp_summary' -ForegroundColor Gray
Write-Host ''

if ($code -ne 0) { exit 1 }
exit 0