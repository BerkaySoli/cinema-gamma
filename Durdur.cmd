@echo off
rem Cinema Gamma'yi kapatir: once kibarca (ekran rengi geri alinir), olmazsa yalnizca
rem cinema_gamma.py calistiran Python surecini sonlandirir. Baska hicbir programa dokunmaz.
if not exist "%LOCALAPPDATA%\CinemaGamma" exit /b 0
type nul > "%LOCALAPPDATA%\CinemaGamma\stop"
powershell -NoProfile -Command "Start-Sleep -Seconds 3; Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'pythonw.exe' -or $_.Name -eq 'python.exe') -and $_.CommandLine -like '*cinema_gamma.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
