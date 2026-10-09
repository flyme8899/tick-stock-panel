# 启动 vendored daily_stock_analysis 的 API。不托管 DSA 自己的前端。
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Dsa = Join-Path $Root 'vendor\daily_stock_analysis'
if (-not (Test-Path (Join-Path $Dsa 'main.py'))) {
    Write-Error "找不到 $Dsa\main.py"
}
$Port = if ($env:DSA_PORT) { $env:DSA_PORT } else { '8000' }
$Bind = if ($env:DSA_HOST) { $env:DSA_HOST } else { '127.0.0.1' }
if (-not $env:ENV_FILE) { $env:ENV_FILE = Join-Path $Root '.env' }
if (-not $env:TZ) { $env:TZ = 'Asia/Shanghai' }
New-Item -ItemType Directory -Force -Path (Join-Path $Root 'data\dsa') | Out-Null
if (-not $env:DATABASE_PATH) {
    $env:DATABASE_PATH = Join-Path $Root 'data\dsa\stock_analysis.db'
}
if (-not $env:TSP_QUANT_EVIDENCE_FILE) {
    $env:TSP_QUANT_EVIDENCE_FILE = Join-Path $Root 'backend\app\custom\dsa\quant_evidence.yaml'
}
$Python = Join-Path $Dsa '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Error '需要 uv 来创建 DSA 虚拟环境'
    }
    & uv venv --python 3.11 (Join-Path $Dsa '.venv')
    & uv pip install --python $Python -r (Join-Path $Dsa 'requirements.txt')
}
$Bootstrap = Join-Path $Root 'backend\app\custom\dsa\dsa_bootstrap.py'
Set-Location $Dsa
& $Python $Bootstrap main.py --serve-only --host $Bind --port $Port
