# 0817_new_dev — ticket 01: real-credential OBS fullchain verification.
#
# Runs the complete acceptance list of ticket 01 in one command:
#   backup OBS object -> ensure old model on OBS -> start service (OBS API mode)
#   -> /health asserts -> baseline infer -> replace OBS object (delete+put)
#   -> infer changed + [hot-reload] in logs -> restart container (startup
#   download path) -> infer unchanged -> restore OBS object.
#
# Usage:
#   .\verify_fullchain.ps1 -Ak <AK> -Sk <SK> [-Bucket xgb-bc-bucket] [-ImageTag xgb-bc:obs-minimal-v5]
#
# OBS state is always restored (or deleted if it did not exist before).

param(
    [Parameter(Mandatory = $true)][string]$Ak,
    [Parameter(Mandatory = $true)][string]$Sk,
    [string]$Bucket = "xgb-bc-bucket",
    [string]$ImageTag = "xgb-bc:obs-minimal-v5",
    [string]$NewModel = "D:\soft\xgboos_demo\0802_start_from_scratch\model_out\new\xgboost_breast_cancer.json"
)

$ErrorActionPreference = "Stop"
$Dir = $PSScriptRoot
$CName = "xgb-0817-fc"
$OldC = "/work/model/xgboost_breast_cancer.json"
$NewC = "/work/new_model_for_test.json"
$BackupC = "/work/obs_backup/xgboost_breast_cancer.json"
$script:Fail = 0

function Check([string]$Name, $Cond) {
    if ($Cond) { Write-Host "  PASS  $Name" -ForegroundColor Green }
    else { Write-Host "  FAIL  $Name" -ForegroundColor Red; $script:Fail++ }
}

function Invoke-ObsTool([string]$Cmd, [string]$File) {
    $a = @("run", "--rm", "-v", "${Dir}:/work",
           "-e", "AccessKeyID=$Ak", "-e", "SecretAccessKey=$Sk", "-e", "OBS_BUCKET=$Bucket",
           $ImageTag, "python", "/work/obs_tool.py", $Cmd)
    if ($File) { $a += @("--file", $File) }
    docker @a
}

function Invoke-Infer() {
    $resp = curl.exe -s -X POST http://127.0.0.1:18081/ `
        -H "Content-Type: application/json" `
        --data-binary "@$Dir\sample_request.json" | ConvertFrom-Json
    [double]$resp[0].predictresult
}

function Get-Health() {
    curl.exe -s http://127.0.0.1:18081/health | ConvertFrom-Json
}

New-Item -ItemType Directory -Force -Path "$Dir\obs_backup" | Out-Null
Copy-Item $NewModel "$Dir\new_model_for_test.json" -Force

try {
    Write-Host "== [1/7] backup current OBS object"
    $backupOut = Invoke-ObsTool "backup" $BackupC
    Write-Host "  $backupOut"
    $hadOriginal = $backupOut -match "EXISTS"

    Write-Host "== [2/7] ensure OLD model on OBS (delete+put)"
    Invoke-ObsTool "replace" $OldC | Write-Host

    Write-Host "== [3/7] start service container (OBS API mode)"
    docker rm -f $CName 2>$null | Out-Null
    docker run -d --name $CName -p 18081:8080 `
        -e OBS_BUCKET=$Bucket -e AccessKeyID=$Ak -e SecretAccessKey=$Sk `
        $ImageTag | Out-Null
    Start-Sleep -Seconds 8
    $h = Get-Health
    Check "sync_mode=obs-api"        ($h.sync_mode -eq "obs-api")
    Check "model_origin=obs"         ($h.model_origin -eq "obs")
    Check "obs.last_check_ok=true"   ($h.obs.last_check_ok -eq $true)

    Write-Host "== [4/7] baseline inference (old model)"
    $predA = Invoke-Infer
    Write-Host "  predA=$predA"

    Write-Host "== [5/7] replace OBS object with NEW model, infer again"
    Invoke-ObsTool "replace" $NewC | Write-Host
    $predB = Invoke-Infer
    Write-Host "  predB=$predB"
    Check "prediction changed (hot swap)" ([math]::Abs($predB - $predA) -gt 1e-6)
    Check "[hot-reload] in logs" ((docker logs $CName 2>&1 | Select-String "\[hot-reload\]") -ne $null)

    Write-Host "== [6/7] restart container -> startup download path"
    docker restart $CName | Out-Null
    Start-Sleep -Seconds 8
    $h2 = Get-Health
    $predC = Invoke-Infer
    Write-Host "  predC=$predC"
    Check "startup download from OBS" ($h2.model_origin -eq "obs")
    Check "prediction stable vs predB" ([math]::Abs($predC - $predB) -lt 1e-9)
}
finally {
    Write-Host "== [7/7] restore OBS object + cleanup"
    if ($hadOriginal) {
        Invoke-ObsTool "replace" $BackupC | Write-Host
        Write-Host "  original object restored"
    } else {
        Invoke-ObsTool "delete" | Out-Null
        Write-Host "  object deleted (did not exist before test)"
    }
    docker rm -f $CName 2>$null | Out-Null
    Remove-Item "$Dir\new_model_for_test.json" -Force -ErrorAction SilentlyContinue
}

Write-Host ""
if ($script:Fail -eq 0) {
    Write-Host "FULLCHAIN: ALL PASS" -ForegroundColor Green
    exit 0
} else {
    Write-Host "FULLCHAIN: $script:Fail FAILURE(S)" -ForegroundColor Red
    exit 1
}
