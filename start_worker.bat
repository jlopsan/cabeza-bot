@echo off
echo ========================================
echo   German Sniper Worker v3 - Launcher
echo ========================================
echo.
echo Este proceso monitorea las misiones activas en segundo plano.
echo - Misiones normales: cada 15 min
echo - Misiones sniper: cada 3 min
echo.
echo NOTA: Puedes ejecutar este worker junto con el bot principal.
echo       El worker usa HTTP directo, NO usa polling.
echo.

REM Start the worker
python worker.py

pause