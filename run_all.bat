@echo off
cd /d "%~dp0"

start "Customer360 Backend" cmd /k call "%~dp0start_backend.bat"

timeout /t 4 /nobreak >nul

start "Customer360 Frontend" cmd /k call "%~dp0start_frontend.bat"

timeout /t 2 /nobreak >nul

start "" "http://127.0.0.1:5500/GhorerBazar_Customer360_Dashboard_v2.html"
