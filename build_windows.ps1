$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

py -3 -m pip install -r requirements.txt
py -3 -m PyInstaller --clean --noconfirm ticket_web.spec

Write-Host "Windows 실행 파일: dist\ticket-sniper.exe"
