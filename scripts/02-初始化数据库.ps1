<#
.SYNOPSIS
    初始化 MySQL 数据库 uav_sched_sim（建库 + 建表 + 灌工况/策略字典），幂等可重复执行。

.DESCRIPTION
    《操作系统》课程实训项目 —— 面向智慧地质勘查的无人机集群并发自适应负载均衡调度仿真系统

    本脚本做三件事：
      1. 连通性预检：确认 MySQL 服务在跑、root 密码可用（密码取自环境变量 UAV_DB_PASSWORD，
         其次取 config.yaml 的 database.password）；
      2. 执行 sql\01_schema.sql：建库 + 8 个表/视图（全部 IF NOT EXISTS，幂等）；
      3. 自检：打印各表行数，确认 scenario_def 3 行、scheduler_def 5 行。

    注意：真正的"每工况参数同步"由 src\uav_sim\db.py 的 sync_scenario_def() 在首次入库时
    以运行期 Scenario 对象为准刷新，保证"库里的参数 == 代码里的参数"。

.PARAMETER Password
    显式指定 MySQL root 密码（优先级最高）。不传则依次取：
    环境变量 UAV_DB_PASSWORD -> config.yaml 的 database.password。

.PARAMETER Host
    MySQL 主机，默认 127.0.0.1。

.PARAMETER Port
    端口，默认 3306。

.EXAMPLE
    .\scripts\02-初始化数据库.ps1
    $env:UAV_DB_PASSWORD='你的密码'; .\scripts\02-初始化数据库.ps1

.NOTES
    执行策略报错时先运行：Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$Password = '',
    [string]$Host = '127.0.0.1',
    [int]$Port = 3306,
    [string]$User = 'root',
    [string]$Database = 'uav_sched_sim'
)

$ErrorActionPreference = 'Stop'

$Root       = Split-Path -Parent $PSScriptRoot
$SchemaFile = Join-Path $Root 'sql\01_schema.sql'
$ConfigFile = Join-Path $Root 'config.yaml'

# MySQL 客户端（本机实测路径；找不到再尝试 PATH）
$MysqlCandidates = @(
    'D:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe',
    'C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe'
)
$Mysql = $MysqlCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Mysql) {
    $cmd = Get-Command mysql.exe -ErrorAction SilentlyContinue
    if ($cmd) { $Mysql = $cmd.Source }
}
if (-not $Mysql) {
    Write-Host '  ✗ 找不到 mysql.exe，请把 MySQL bin 目录加入 PATH 或用 -MysqlPath 指定（本脚本需手工改）' -ForegroundColor Red
    exit 2
}

function Write-Step { param([string]$Text)
    Write-Host ''
    Write-Host ('=' * 72) -ForegroundColor DarkCyan
    Write-Host ("  " + $Text) -ForegroundColor Cyan
    Write-Host ('=' * 72) -ForegroundColor DarkCyan
}

# ---------------------------------------------------------------------------
Write-Step '1 / 3   准备连接参数'
Write-Host ("  mysql 客户端 : " + $Mysql)
Write-Host ("  目标         : " + $User + "@" + $Host + ":" + $Port + " / 库 " + $Database)

# 密码优先级：参数 > 环境变量 > config.yaml
if (-not $Password) { $Password = $env:UAV_DB_PASSWORD }
if (-not $Password -and (Test-Path $ConfigFile)) {
    # 只做最小解析，避免为一次建库引入 YAML 依赖
    $line = Select-String -Path $ConfigFile -Pattern '^\s*password:\s*(.+)$' | Select-Object -First 1
    if ($line) {
        $Password = $line.Matches[0].Groups[1].Value.Trim().Trim('"').Trim("'")
    }
}
if (-not $Password) {
    Write-Host '  ✗ 未能取得数据库密码。请设置环境变量 UAV_DB_PASSWORD，或用 -Password 传入。' -ForegroundColor Red
    exit 2
}
Write-Host '  密码来源     : ' -NoNewline
if ($env:UAV_DB_PASSWORD -eq $Password) { Write-Host '环境变量 UAV_DB_PASSWORD' } else { Write-Host 'config.yaml / 参数' }

if (-not (Test-Path $SchemaFile)) {
    Write-Host ("  ✗ 建表脚本不存在：" + $SchemaFile) -ForegroundColor Red
    exit 2
}

# ---------------------------------------------------------------------------
Write-Step '2 / 3   连通性预检'
$env:MYSQL_PWD = $Password   # 避免密码出现在命令行（防 ps 泄露）
try {
    $ver = & $Mysql --host=$Host --port=$Port --user=$User --silent --skip-column-names `
        -e "SELECT VERSION();" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host ("  ✗ 连接失败：" + ($ver -join ' ')) -ForegroundColor Red
        Write-Host '    排查：1) MySQL 服务是否启动（Get-Service MySQL）' -ForegroundColor Yellow
        Write-Host '          2) 密码是否正确（环境变量 UAV_DB_PASSWORD）' -ForegroundColor Yellow
        Write-Host '          3) 端口 3306 是否被占用/防火墙拦截' -ForegroundColor Yellow
        exit 1
    }
    Write-Host ("  ✓ MySQL 版本 " + ($ver -join '')) -ForegroundColor Green
} finally {
    # 保持 MYSQL_PWD 到建库完成后再清除
}

# ---------------------------------------------------------------------------
Write-Step '3 / 3   执行建库建表脚本（幂等）'
Write-Host ("  脚本：" + $SchemaFile)
& $Mysql --host=$Host --port=$Port --user=$User --default-character-set=utf8mb4 `
    --comments --verbose < $SchemaFile
if ($LASTEXITCODE -ne 0) {
    Remove-Item Env:\MYSQL_PWD -ErrorAction SilentlyContinue
    Write-Host '  ✗ 建库失败，请检查上面的 SQL 报错。' -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '  —— 自检 ——' -ForegroundColor Cyan
& $Mysql --host=$Host --port=$Port --user=$User --database=$Database --silent `
    -e "SELECT CONCAT('  scenario_def   ', COUNT(*), ' 行（期望 3）') FROM scenario_def;
        SELECT CONCAT('  scheduler_def  ', COUNT(*), ' 行（期望 5）') FROM scheduler_def;
        SELECT CONCAT('  experiment     ', COUNT(*), ' 行（跑实验后会增长）') FROM experiment;
        SELECT CONCAT('  exp_metric     ', COUNT(*), ' 行') FROM exp_metric;
        SELECT CONCAT('  task_record    ', COUNT(*), ' 行') FROM task_record;
        SELECT CONCAT('  queue_sample   ', COUNT(*), ' 行') FROM queue_sample;
        SELECT CONCAT('  link_sample    ', COUNT(*), ' 行') FROM link_sample;"
$rc = $LASTEXITCODE
Remove-Item Env:\MYSQL_PWD -ErrorAction SilentlyContinue

if ($rc -ne 0) {
    Write-Host '  ✗ 自检查询失败' -ForegroundColor Red
    exit 1
}

Write-Host ''
Write-Host '  ✓ 数据库就绪。接下来：' -ForegroundColor Green
Write-Host '      .\.venv\Scripts\python.exe run.py all        # 跑 15 组实验并入库出图'
Write-Host '      mysql --host=127.0.0.1 -uroot -p uav_sched_sim -e "select * from v_exp_summary;"'
Write-Host ''
exit 0
