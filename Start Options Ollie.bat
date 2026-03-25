@echo off
:: Options Ollie — Windows Launcher
:: Double-click this file to start the web interface

cd /d "%~dp0"

echo.
echo  Options Ollie - Starting Web Interface...
echo.

echo Checking dependencies...
pip install flask yfinance scipy --quiet

echo Starting server...
echo.
echo Once started, open your browser to: http://localhost:5000
echo (Close this window to stop the server)
echo.

python server.py
pause
