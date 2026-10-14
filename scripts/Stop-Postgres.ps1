param([ValidatePattern('^[a-z][a-z0-9-]*$')][string]$Cluster = 'main')
$ErrorActionPreference = 'Stop'
. $PSScriptRoot\Environment.ps1
$ShopData = Join-Path $ShopRoot ('data\postgresql\' + $Cluster)
& (Join-Path $ShopPgBin 'pg_ctl.exe') -D $ShopData -m fast -w stop
if ($LASTEXITCODE -ne 0) { throw '数据库停止失败' }
