param([ValidateRange(1024,65535)][int]$Port = 5433,
      [ValidatePattern('^[a-z][a-z0-9-]*$')][string]$Cluster = 'main')
$ErrorActionPreference = 'Stop'
. $PSScriptRoot\Environment.ps1
if (-not (Test-Path -LiteralPath (Join-Path $ShopPgBin 'pg_ctl.exe'))) { throw '缺少 PostgreSQL 二进制文件，见 PostgreSQL运行与隔离验证.md' }
$ShopData = Join-Path $ShopRoot ('data\postgresql\' + $Cluster)
& $ShopPython (Join-Path $PSScriptRoot 'bootstrap_postgres.py') credentials
if ($LASTEXITCODE -ne 0) { throw '凭据初始化失败' }
if (-not (Test-Path -LiteralPath (Join-Path $ShopData 'PG_VERSION'))) {
    $ShopPasswordFile = Join-Path $ShopRoot '.local\init-password'
    & (Join-Path $ShopPgBin 'initdb.exe') -D $ShopData -U postgres --encoding=UTF8 --locale=C --auth=scram-sha-256 --pwfile $ShopPasswordFile
    if ($LASTEXITCODE -ne 0) { throw '数据库集群初始化失败' }
}
& (Join-Path $ShopPgBin 'pg_ctl.exe') -D $ShopData status
if ($LASTEXITCODE -eq 0) {
    $ShopRunningPort = (Get-Content -LiteralPath (Join-Path $ShopData 'postmaster.pid'))[3]
    if ([int]$ShopRunningPort -ne $Port) { throw ('该集群已在端口 ' + $ShopRunningPort + ' 运行') }
} else {
    $ShopLogDirectory = Join-Path $ShopRoot 'data\postgresql\logs'
    New-Item -ItemType Directory -Path $ShopLogDirectory -Force | Out-Null
    $ShopLog = Join-Path $ShopLogDirectory ($Cluster + '.log')
    $ShopArguments = @('-D', ('"' + $ShopData + '"'), '-l', ('"' + $ShopLog + '"'), '-o', ('"-p ' + $Port + ' -h 127.0.0.1"'), '-w', 'start')
    $ShopProcess = Start-Process -FilePath (Join-Path $ShopPgBin 'pg_ctl.exe') -ArgumentList $ShopArguments -WindowStyle Hidden -PassThru
    if (-not $ShopProcess.WaitForExit(30000)) { throw ('数据库启动等待超时，请检查 ' + $ShopLog) }
    if ($ShopProcess.ExitCode -ne 0) { throw ('数据库启动失败，查看 ' + $ShopLog) }
}
& $ShopPython (Join-Path $PSScriptRoot 'bootstrap_postgres.py') provision --port $Port
if ($LASTEXITCODE -ne 0) { throw '数据库账户配置失败' }
$env:PGPORT = [string]$Port
Write-Output ('数据库目录：' + $ShopData)
