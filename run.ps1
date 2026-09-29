# Long-run Monitor - script chay nhanh tren Windows PowerShell
#
#   .\run.ps1                 -> mo GUI
#   .\run.ps1 -Headless       -> chay khong GUI toi khi Ctrl+C
#   .\run.ps1 -Headless -Hours 72
#   .\run.ps1 -Install        -> chi cai dependency roi thoat
#   .\run.ps1 -Test           -> chay smoke test (logic + GUI), khong can mang
#   .\run.ps1 -NoInstall      -> bo qua buoc pip install
#
# Neu bi chan execution policy:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
#
# LUU Y: file nay CO TINH chi dung ky tu ASCII. PowerShell 5.1 doc .ps1 khong co
# BOM bang codepage ANSI cua Windows, nen dau tieng Viet se bi doc sai va lam vo
# cu phap script. Can thong bao tieng Viet co dau thi sua o README.md, khong o day.

param(
    [string]$Python = "python",
    [switch]$Headless,
    [double]$Hours = 0,
    [string]$LogLevel = "INFO",
    [switch]$Install,
    [switch]$NoInstall,
    [switch]$Test
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not $NoInstall) {
    Write-Host "==> Cai dependency (requirements.txt)..." -ForegroundColor Cyan
    & $Python -m pip install --disable-pip-version-check -r requirements.txt
}

if ($Install) {
    Write-Host "==> Da cai xong." -ForegroundColor Green
    exit 0
}

# Console Windows mac dinh cp1252 -> script Python in tieng Viet se loi
# UnicodeEncodeError neu khong bat UTF-8.
$env:PYTHONUTF8 = "1"

if ($Test) {
    Write-Host "==> Chay smoke test (khong can mang)..." -ForegroundColor Cyan
    # LUU Y: python ghi CANH BAO ra stderr (vd PyQt5 offscreen bao thieu thu muc
    # font). Voi $ErrorActionPreference = "Stop" thi PowerShell 5.1 coi dong stderr
    # cua chuong trinh native la loi nghiem trong -> script DUNG NGAY, test GUI
    # khong bao gio chay. Ket qua dung/sai phai lay tu $LASTEXITCODE.
    $ErrorActionPreference = "Continue"
    & $Python "tests/smoke_test.py"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Python "tests/gui_smoke_test.py"
    exit $LASTEXITCODE
}

$argsList = @("main.py", "--log-level", $LogLevel)
if ($Headless) {
    $argsList += "--headless"
    if ($Hours -gt 0) { $argsList += @("--hours", "$Hours") }
    Write-Host "==> Chay KHONG GUI. Dung bang Ctrl+C (se ghi not du lieu + bao cao)." -ForegroundColor Yellow
} else {
    Write-Host "==> Mo GUI..." -ForegroundColor Cyan
}

& $Python @argsList
exit $LASTEXITCODE
