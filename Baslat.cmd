@echo off
rem Cinema Gamma: Eye-CareU yanindaki paneli konsol penceresi olmadan baslatir.
setlocal
set "SCRIPT=%~dp0cinema_gamma.py"
set "PYW=%LOCALAPPDATA%\Programs\Python\Python39\pythonw.exe"
if not exist "%PYW%" for %%P in (pythonw.exe) do set "PYW=%%~$PATH:P"
rem Python PATH'te degilse Windows'un py baslaticisini (pyw.exe) dene.
if not defined PYW for %%P in (pyw.exe) do set "PYW=%%~$PATH:P"
if not defined PYW (
    echo Python bulunamadi. python.org adresinden Python 3.9 veya daha yenisini kurun.
    pause
    exit /b 1
)
start "" "%PYW%" "%SCRIPT%"
