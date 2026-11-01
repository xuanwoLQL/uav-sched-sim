@echo off
chcp 65001 >nul
title 无人机集群并发自适应调度仿真系统
cd /d "%~dp0"

echo.
echo ============================================================
echo   无人机集群并发自适应调度仿真系统 v1.0
echo   智慧地质勘查 · 3D 实时可视化
echo ============================================================
echo.

if not exist ".venv\Scripts\pythonw.exe" goto NOVENV

rem 优先用 pythonw（无控制台窗口）；失败则用 python 带控制台便于看报错
start "" ".venv\Scripts\pythonw.exe" app.py
exit /b 0

:NOVENV
echo [错误] 没找到虚拟环境 .venv
echo        请先运行： scripts\01-创建虚拟环境.ps1
echo.
pause
exit /b 1
