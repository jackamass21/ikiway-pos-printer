@echo off
title Compilar Ikiway POS Printer
color 0B
cd /d %~dp0

echo ==========================================
echo   COMPILAR IKIWAY POS PRINTER PARA WINDOWS
echo ==========================================
echo.

where node >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Instala Node.js 20 o superior.
    pause
    exit /b 1
)

call npm ci
if errorlevel 1 (
    echo [ERROR] No se pudieron instalar las dependencias.
    pause
    exit /b 1
)

call npm run dist:win
if errorlevel 1 (
    echo [ERROR] No se pudo compilar la aplicacion.
    pause
    exit /b 1
)

echo.
echo [OK] Ejecutables generados en la carpeta dist:
dir /b dist\*.exe
echo.
pause
