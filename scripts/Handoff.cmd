@echo off
rem agent-handoff entry point for Codex and plain cmd.exe.
rem The real interface is Python; this launcher only finds Python and forwards.
rem Batch files are not affected by the PowerShell execution policy that blocks .ps1.
setlocal
where node >nul 2>nul
if errorlevel 1 (
    echo Node.js was not found on PATH.
    echo Use the Python interface directly, for example:
    echo   python .agents\skills\agent-handoff\scripts\doctor.py
    exit /b 2
)
node "%~dp0handoff.mjs" %*
exit /b %ERRORLEVEL%
