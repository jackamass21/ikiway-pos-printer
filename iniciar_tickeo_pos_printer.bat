@echo off
title Ikiway POS Printer Agent
cd /d %~dp0

echo ==========================================
echo   IKIWAY POS PRINTER AGENT - ESC/POS USB
echo ==========================================
echo.

where node >nul 2>nul
if errorlevel 1 (
    echo ERROR: Node.js no esta instalado o no esta en PATH.
    echo Instala Node.js 20 o superior y vuelve a ejecutar este archivo.
    pause
    exit /b 1
)

node -e "process.exit(Number(process.versions.node.split('.')[0]) >= 20 ? 0 : 1)"
if errorlevel 1 (
    echo ERROR: Se requiere Node.js 20 o superior.
    node -v
    pause
    exit /b 1
)

if not exist node_modules (
    echo ERROR: Faltan dependencias. Ejecuta instalar_tickeo_pos_printer.bat.
    pause
    exit /b 1
)

if not exist .env (
    echo AVISO: No existe .env. Se usaran solo los origenes locales de desarrollo.
    echo Copia .env.example como .env y configura el dominio del POS.
    echo.
)

echo Iniciando agente de impresion...
echo Estado: http://127.0.0.1:17892/health
echo.
npm start

echo.
echo El agente se detuvo.
pause
