@echo off
echo ========================================
echo   German Sniper Bot v3 - Launcher
echo ========================================
echo.

REM Kill any existing Python processes to avoid polling conflicts
echo [1/3] Deteniendo procesos de Python existentes...
taskkill /F /IM python.exe 2>nul
if %errorlevel% equ 0 (
    echo       Procesos de Python detenidos.
) else (
    echo       No se encontraron procesos de Python en ejecucion.
)
echo.

REM Wait a moment for Telegram API to release the connection
echo [2/3] Esperando 3 segundos para liberar conexion con Telegram...
timeout /t 3 /nobreak >nul
echo.

REM Start the bot
echo [3/3] Iniciando German Sniper Bot v3...
echo       Presiona Ctrl+C para detener el bot.
echo.
python main.py

pause