@echo off
title Instalador Ikiway POS Printer Agent
color 0A
cd /d %~dp0

echo ==========================================
echo   INSTALADOR IKIWAY POS PRINTER AGENT
echo ==========================================
echo.

where node >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Node.js no esta instalado o no esta en PATH.
    echo Descarga Node.js 20 o superior desde https://nodejs.org/
    pause
    exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
    echo [ERROR] npm no esta disponible. Reinstala Node.js.
    pause
    exit /b 1
)

node -e "process.exit(Number(process.versions.node.split('.')[0]) >= 20 ? 0 : 1)"
if errorlevel 1 (
    echo [ERROR] Se requiere Node.js 20 o superior.
    node -v
    pause
    exit /b 1
)

if not exist package.json (
    echo [ERROR] Ejecuta este instalador dentro de la carpeta del agente.
    pause
    exit /b 1
)

echo Instalando versiones verificadas desde package-lock.json...
call npm ci
if errorlevel 1 (
    echo [ERROR] No se pudieron instalar las dependencias.
    pause
    exit /b 1
)

echo Ejecutando pruebas del comprobante y PDF417...
call npm test
if errorlevel 1 (
    echo [ERROR] Las pruebas del agente fallaron.
    pause
    exit /b 1
)

if not exist .env if exist .env.example copy .env.example .env >nul

echo.
echo [OK] Instalacion finalizada.
echo Edita .env para autorizar el dominio del POS y luego ejecuta:
echo iniciar_tickeo_pos_printer.bat
echo.
pause
