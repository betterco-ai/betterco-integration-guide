@echo off
title Mandanten-Neuannahme Widget :8770
color 1F
echo ============================================
echo   Mandanten-Neuannahme PoC  -  Suchwidget
echo ============================================
echo.

rem Free port 8770 before starting
powershell -NoProfile -Command "try { $ids = (Get-NetTCPConnection -LocalPort 8770 -State Listen -ErrorAction Stop).OwningProcess | Sort-Object -Unique; foreach($id in $ids){ Start-Process -FilePath taskkill -ArgumentList '/F','/PID',$id -NoNewWindow -Wait -ErrorAction SilentlyContinue } } catch {}"
timeout /t 2 /nobreak >nul

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem Pass a workspace env via --env-file, default is workspaces/editor-betterco-claude.env
python "%~dp0poc_search_app.py" --port 8770 %*

pause
