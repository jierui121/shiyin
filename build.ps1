$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& '.\.venv\Scripts\python.exe' 'packaging\make_icon.py'
if ($LASTEXITCODE -ne 0) { throw 'Icon generation failed.' }
& '.\.venv\Scripts\python.exe' -m PyInstaller --noconfirm 'packaging\shiyin.spec'
if ($LASTEXITCODE -ne 0) { throw 'Application build failed.' }
& '.\.venv\Scripts\python.exe' 'packaging\release_bundle.py'
if ($LASTEXITCODE -ne 0) { throw 'Release packaging failed.' }
