$ErrorActionPreference = 'Stop'
$ShopRoot = Split-Path -Parent $PSScriptRoot
Push-Location $ShopRoot
try {
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        py -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python 虚拟环境创建失败' }
    }
    . $PSScriptRoot\Environment.ps1
    & $ShopPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
    if (-not (Test-Path -LiteralPath (Join-Path $ShopPgBin 'postgres.exe'))) {
        New-Item -ItemType Directory -Path '.tmp', '.tools' -Force | Out-Null
        $ShopArchive = Join-Path $ShopRoot '.tmp\postgresql-17.11-windows.zip'
        # EDB Windows binary archive linked by the official download page.
        Invoke-WebRequest 'https://sbp.enterprisedb.com/getfile.jsp?fileid=1260616' -OutFile $ShopArchive
        Expand-Archive -LiteralPath $ShopArchive -DestinationPath (Join-Path $ShopRoot '.tools\postgresql') -Force
    }
    & (Join-Path $ShopPgBin 'postgres.exe') --version
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL 运行时检查失败' }
    Write-Output '环境就绪。接下来运行 scripts\Start-Postgres.ps1，再迁移或初始化数据库。'
}
finally { Pop-Location }
