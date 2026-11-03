@echo off
REM stop-tvapp.cmd - stops whatever is listening on the tvapp port
setlocal
set PORT=8777
if not "%~1"=="" set PORT=%~1
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$c = Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue; if ($c) { $c | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }; Write-Host ('stopped tvapp on port %PORT%') } else { Write-Host ('nothing listening on port %PORT%') }"
endlocal
