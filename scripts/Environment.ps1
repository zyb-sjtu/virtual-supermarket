$ShopRoot = Split-Path -Parent $PSScriptRoot
$ShopPython = Join-Path $ShopRoot '.venv\Scripts\python.exe'
$ShopPgBin = Join-Path $ShopRoot '.tools\postgresql\pgsql\bin'
if (-not (Test-Path -LiteralPath $ShopPython)) { throw '请先创建 .venv 并安装 requirements.txt' }
# A venv created from this computer's Conda Python needs its native DLL path.
$ShopBasePrefix = (& $ShopPython -c 'import sys; print(sys.base_prefix)').Trim()
$ShopDllDirectory = Join-Path $ShopBasePrefix 'Library\bin'
if (Test-Path -LiteralPath $ShopDllDirectory) { $env:PATH = $ShopDllDirectory + ';' + $env:PATH }
$env:PYTHONIOENCODING = 'utf-8'
$env:SHOP_DB_BACKEND = 'postgresql'
$env:PGHOST = '127.0.0.1'
$env:PGUSER = 'shop_app'
$env:PGDATABASE = 'shop'
$env:PGPASSFILE = Join-Path $ShopRoot '.local\pgpass.conf'
if (-not $env:PGPORT) { $env:PGPORT = '5433' }
