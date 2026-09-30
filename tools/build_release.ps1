# Test Gateway IoT - dong goi ban phat hanh (.exe + JSON) de up len git
#
#   .\tools\build_release.ps1              -> build exe + gom vao release/
#   .\tools\build_release.ps1 -SkipBuild   -> chi gom lai release/ tu dist/
#   .\tools\build_release.ps1 -Python C:\python314\python.exe
#
# Ket qua: release\<version>\
#   TestGatewayIoT.exe   ban chay doc lap, KHONG can cai Python
#   config/              config.json + devices.json mau (nguoi dung sua truc tiep)
#   docs/                danh muc gateway nha may
#   HUONG_DAN_SU_DUNG.md huong dan su dung
#   README.md
#   release.json         manifest: phien ban, kich thuoc, ma bam SHA-256
#
# LUU Y: file nay CO TINH chi dung ky tu ASCII (xem giai thich trong run.ps1).

param(
    [string]$Python = "python",
    [string]$Version = "",
    [switch]$SkipBuild,
    [switch]$NoZip
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# --------------------------------------------------------------------------
# Phien ban: lay tu core/__init__.py neu khong truyen tham so
# --------------------------------------------------------------------------
if (-not $Version) {
    $initFile = Join-Path $Root "core\__init__.py"
    $line = Select-String -Path $initFile -Pattern '__version__\s*=\s*"([^"]+)"' |
        Select-Object -First 1
    if (-not $line) { throw "Khong doc duoc __version__ tu $initFile" }
    $Version = $line.Matches[0].Groups[1].Value
}
Write-Host "==> Phien ban: $Version" -ForegroundColor Cyan

# --------------------------------------------------------------------------
# 1. Build exe
# --------------------------------------------------------------------------
if (-not $SkipBuild) {
    Write-Host "==> Build exe (PyInstaller)..." -ForegroundColor Cyan
    & $Python -m PyInstaller --clean --noconfirm TestGatewayIoT.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller that bai (exit $LASTEXITCODE)" }
} else {
    Write-Host "==> Bo qua build, dung lai dist\TestGatewayIoT.exe" -ForegroundColor Yellow
}

$exe = Join-Path $Root "dist\TestGatewayIoT.exe"
if (-not (Test-Path $exe)) { throw "Khong tim thay $exe" }

# --------------------------------------------------------------------------
# 2. Gom goi phat hanh
# --------------------------------------------------------------------------
$outDir = Join-Path $Root "release\$Version"
if (Test-Path $outDir) { Remove-Item $outDir -Recurse -Force }
New-Item -ItemType Directory -Path $outDir | Out-Null

Copy-Item $exe $outDir
New-Item -ItemType Directory -Path (Join-Path $outDir "config") | Out-Null
New-Item -ItemType Directory -Path (Join-Path $outDir "docs") | Out-Null
Copy-Item "config\config.json"  (Join-Path $outDir "config")
Copy-Item "config\devices.json" (Join-Path $outDir "config")
Copy-Item "docs\Danh sach 55 gateway.csv" (Join-Path $outDir "docs")
Copy-Item "HUONG_DAN_SU_DUNG.md" $outDir
Copy-Item "README.md" $outDir

# --------------------------------------------------------------------------
# 3. Manifest JSON (de nguoi nhan biet dang cam ban nao, va kiem tra file nguyen ven)
# --------------------------------------------------------------------------
$exeItem = Get-Item (Join-Path $outDir "TestGatewayIoT.exe")
$jsonExe = [ordered]@{
    path   = "TestGatewayIoT.exe"
    bytes  = $exeItem.Length
    sha256 = (Get-FileHash $exeItem.FullName -Algorithm SHA256).Hash.ToLower()
}

function Get-JsonFileInfo {
    param([string]$Relative)
    $item = Get-Item (Join-Path $outDir $Relative)
    return [ordered]@{
        path   = $Relative.Replace("\", "/")
        bytes  = $item.Length
        sha256 = (Get-FileHash $item.FullName -Algorithm SHA256).Hash.ToLower()
    }
}

$manifest = [ordered]@{
    schema        = 1
    tool          = "Test Gateway IoT"
    cli           = "test_gateway_iot"
    version       = $Version
    built_at      = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    platform      = "windows-x64"
    entrypoint    = "TestGatewayIoT.exe"
    python_needed = $false
    network       = [ordered]@{
        note       = "Chi dung TCP/IP thuong (MQTT broker + ICMP ping). KHONG dung 4G/3G/cellular."
        mqtt_host  = (Get-Content "config\config.json" -Raw | ConvertFrom-Json).mqtt_host
        mqtt_port  = (Get-Content "config\config.json" -Raw | ConvertFrom-Json).mqtt_port
        requires   = "PC phai thay duoc broker va cac gateway qua LAN/Ethernet (vd 192.168.10.x)"
    }
    usage         = [ordered]@{
        gui      = "TestGatewayIoT.exe"
        headless = "TestGatewayIoT.exe --headless --hours 72"
        version  = "TestGatewayIoT.exe --version"
    }
    files         = @(
        $jsonExe,
        (Get-JsonFileInfo "config\config.json"),
        (Get-JsonFileInfo "config\devices.json"),
        (Get-JsonFileInfo "docs\Danh sach 55 gateway.csv"),
        (Get-JsonFileInfo "HUONG_DAN_SU_DUNG.md"),
        (Get-JsonFileInfo "README.md")
    )
}

$manifestPath = Join-Path $outDir "release.json"
$manifest | ConvertTo-Json -Depth 6 | Set-Content -Path $manifestPath -Encoding UTF8
Write-Host "==> Da ghi $manifestPath" -ForegroundColor Green

# --------------------------------------------------------------------------
# 4. ZIP (tien gui kem / dinh vao GitHub Release)
# --------------------------------------------------------------------------
if (-not $NoZip) {
    $zip = Join-Path $Root "release\TestGatewayIoT-$Version-win-x64.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path (Join-Path $outDir "*") -DestinationPath $zip
    $zipMB = [math]::Round((Get-Item $zip).Length / 1MB, 1)
    Write-Host "==> Da ghi $zip ($zipMB MB)" -ForegroundColor Green
}

Write-Host ""
Write-Host "==> XONG. Goi phat hanh:" -ForegroundColor Green
Get-ChildItem $outDir | ForEach-Object {
    $size = if ($_.PSIsContainer) { "<DIR>" } else { "{0,8:N1} MB" -f ($_.Length / 1MB) }
    Write-Host ("    {0,14}  {1}" -f $size, $_.Name)
}
Write-Host ""
Write-Host "Up len git:  git add release; git commit -m 'release: $Version'; git push"
