#!/bin/bash
# Options Ollie — Mac Launcher
# Double-click this file to start the web interface

cd "$(dirname "$0")"

echo ""
echo "🎯 Options Ollie — Starting Web Interface..."
echo ""

# Install dependencies if needed
echo "Checking dependencies..."
pip install flask yfinance scipy --quiet 2>/dev/null || pip3 install flask yfinance scipy --quiet 2>/dev/null

echo "Starting server..."
echo ""
echo "Once started, open your browser to: http://localhost:5000"
echo "(Press Ctrl+C in this window to stop)"
echo ""

python3 server.py || python server.py
