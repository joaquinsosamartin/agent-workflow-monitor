$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --name agent-monitor --collect-all uvicorn --add-data 'monitor/static;monitor/static' --paths . entry.py
Write-Host 'Executable: dist\agent-monitor.exe'
