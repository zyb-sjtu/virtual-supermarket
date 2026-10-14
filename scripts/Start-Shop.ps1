param([ValidateRange(1024,65535)][int]$DatabasePort = 5433,
      [ValidateRange(1024,65535)][int]$WebPort = 8000)
$ErrorActionPreference = 'Stop'
. $PSScriptRoot\Environment.ps1
$env:PGPORT = [string]$DatabasePort
$env:SHOP_PORT = [string]$WebPort
Push-Location $ShopRoot
try { & $ShopPython server.py }
finally { Pop-Location }
