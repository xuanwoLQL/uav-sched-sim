<#
.SYNOPSIS
    构建 GPU 重复实验内核（replication_kernel.cu）。

.DESCRIPTION
    本机（2026-11 实测）环境：
      * CUDA Toolkit 13.3  ->  D:\CUDA Toolkit for hashcat\bin\nvcc.exe
      * MSVC 14.51 (VS2022)->  D:\Reverse tools\Visual Studio 2022\visual studio 2022
    两者都不在 PATH 里，所以本脚本负责把 nvcc 的编译环境拼出来
    （设置 PATH / INCLUDE / LIB 三个变量），再调用 nvcc。

.PARAMETER Rebuild
    强制重新编译（默认：exe 比 .cu 新就跳过）。

.EXAMPLE
    .\gpu\build.ps1
    .\gpu\build.ps1 -Rebuild
#>
[CmdletBinding()]
param(
    [switch]$Rebuild
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here

# ---- 1) 找 nvcc ----
$nvccCandidates = @(
    'D:\CUDA Toolkit for hashcat\bin\nvcc.exe',
    "$env:CUDA_PATH\bin\nvcc.exe"
)
$nvcc = $null
foreach ($c in $nvccCandidates) { if ($c -and (Test-Path $c)) { $nvcc = $c; break } }
if (-not $nvcc) {
    $found = Get-Command nvcc.exe -ErrorAction SilentlyContinue
    if ($found) { $nvcc = $found.Source }
}
if (-not $nvcc) {
    throw "找不到 nvcc.exe。请把 CUDA Toolkit 的 bin 目录加入 PATH，或修改本脚本的 nvccCandidates。"
}
$cudaRoot = Split-Path -Parent (Split-Path -Parent $nvcc)

# ---- 2) 找 MSVC（cl.exe 需要 INCLUDE/LIB） ----
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$vsRoot = $null
if (Test-Path $vswhere) {
    $vsRoot = (& $vswhere -latest -products * -property installationPath) 2>$null
}
if (-not $vsRoot) {
    # 本机实测路径（VS 装在 D 盘）
    foreach ($p in @('D:\Reverse tools\Visual Studio 2022\visual studio 2022',
                     'C:\Program Files\Microsoft Visual Studio\2022\Community')) {
        if (Test-Path $p) { $vsRoot = $p; break }
    }
}
if (-not $vsRoot) { throw "找不到 Visual Studio (MSVC)。nvcc 需要 cl.exe 作为 host 编译器。" }

$msvcBin = Get-ChildItem -Path (Join-Path $vsRoot 'VC\Tools\MSVC') -Directory |
           Sort-Object Name -Descending | Select-Object -First 1
if (-not $msvcBin) { throw "在 $vsRoot\VC\Tools\MSVC 下找不到 MSVC 工具集。" }
$msvc = $msvcBin.FullName

Write-Host "[1/3] nvcc    : $nvcc"
Write-Host "[2/3] MSVC    : $msvc"

# ---- 3) 拼编译环境 ----
$env:PATH = "$cudaRoot\bin;$msvc\bin\Hostx64\x64;$env:PATH"
$env:INCLUDE = "$msvc\include;$cudaRoot\include;$env:INCLUDE"
$env:LIB = "$msvc\lib\x64;$cudaRoot\lib\x64;$env:LIB"

# ---- 目标架构：优先用本机 GPU 的真实算力 ----
$arch = 'sm_86'   # RTX 3050 Laptop = Ampere = 8.6；其它卡可改这里
try {
    $cc = (& nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>$null | Select-Object -First 1)
    if ($cc) { $arch = 'sm_' + ($cc.Trim() -replace '\.', '') }
} catch { }

$src = Join-Path $here 'replication_kernel.cu'
$out = Join-Path $here 'replication_kernel.exe'

if ((-not $Rebuild) -and (Test-Path $out) -and
    ((Get-Item $out).LastWriteTime -gt (Get-Item $src).LastWriteTime)) {
    Write-Host "[3/3] 已是最新：$out（用 -Rebuild 强制重建）"
    exit 0
}

Write-Host "[3/3] 编译中：-O3 -arch=$arch ..."
# 注意：PowerShell 直接写 -arch=$arch 有时不会展开变量（会原样传给 nvcc），
# 因此显式拼成字符串再传参。
$archFlag = "-arch=$arch"
$diagFlag = '--diag-suppress=177,20012'
# 关键：源码注释是 UTF-8 中文，但 MSVC 默认按系统代码页(936/GBK)读文件，
# 中文被误解码后可能产生 0x5C('\\') 尾字节 —— 那会被当成"行继续符"，
# 把紧跟的下一行代码吞掉（表现为莫名其妙的"struct 没有这个成员"）。
# 因此必须显式告诉 host 编译器：源文件是 UTF-8。
$utf8Flag = '-Xcompiler=/utf-8'
# nvcc 会把 CUDA 头文件的代码页警告打到 stderr；若此时 ErrorActionPreference=Stop
# 会被 PowerShell 当成致命错误。这里临时放宽，收集输出后再自行判定退出码。
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$nvccOut = & $nvcc -O3 $archFlag $utf8Flag -o $out $src $diagFlag 2>&1
$nvccCode = $LASTEXITCODE
$ErrorActionPreference = $prevEAP

$nvccOut | Where-Object { $_ -and ($_ -notmatch 'C4819') } | ForEach-Object { Write-Host $_ }
if ($nvccCode -ne 0) { throw "编译失败（退出码 $nvccCode）" }
Write-Host "构建成功：$out"
