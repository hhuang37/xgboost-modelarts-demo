# 0817_new_dev — build / local-verify / push helper for the unified image.
#
# Usage:
#   .\build_and_run.ps1              # build only
#   .\build_and_run.ps1 test-local   # build + run local-signature mode (no OBS creds), curl /health + infer
#   .\build_and_run.ps1 test-obs     # build + run OBS API mode with real AK/SK (fill in below first)
#   .\build_and_run.ps1 push         # tag + push to SWR (fill in $SwrRepo first)
#
# NOTE: --provenance=false is REQUIRED. Without it buildx emits an OCI Image
# Index that SWR/ModelArts rejects with
# "Invalid image, fail to parse 'manifest.json'".

param(
    [Parameter(Position = 0)]
    [ValidateSet("", "test-local", "test-obs", "push")]
    [string]$Action = "",

    [string]$ImageTag = "xgb-bc:obs-minimal-v5",

    # For "push": full SWR repo, e.g. swr.cn-north-4.myhuaweicloud.com/hhuang37/xgb-bc
    [string]$SwrRepo = "",

    # For "test-obs": fill in real credentials (never commit these).
    [string]$ObsBucket = "xgb-bc-bucket",
    [string]$Ak = "",
    [string]$Sk = ""
)

$ErrorActionPreference = "Stop"

Write-Host "== build $ImageTag"
docker buildx build --platform linux/amd64 --provenance=false -t $ImageTag .

if ($Action -ne "test-local" -and $Action -ne "test-obs") {
    if ($Action -eq "push") {
        if (-not $SwrRepo) { throw "push requires -SwrRepo swr.cn-north-4.myhuaweicloud.com/<org>/<repo>" }
        $Full = "$SwrRepo`:$ImageTag".Replace(":xgb-bc:", ":")
        docker tag $ImageTag "$SwrRepo`:obs-minimal-v5-0817"
        docker push "$SwrRepo`:obs-minimal-v5-0817"
    }
    exit 0
}

Write-Host "== run container"
docker rm -f xgb-0817-test 2>$null
if ($Action -eq "test-local") {
    # Local-signature mode: no OBS creds -> baked-in model, (mtime,size) watch.
    docker run --rm -d --name xgb-0817-test -p 18081:8080 $ImageTag
} else {
    if (-not $Ak -or -not $Sk) { throw "test-obs requires -Ak and -Sk" }
    # OBS API mode: startup probe + per-request hot swap.
    docker run --rm -d --name xgb-0817-test -p 18081:8080 `
        -e OBS_BUCKET=$ObsBucket `
        -e AccessKeyID=$Ak `
        -e SecretAccessKey=$Sk `
        $ImageTag
}

Start-Sleep -Seconds 5
Write-Host "`n== /health"
curl.exe -s http://127.0.0.1:18081/health
Write-Host "`n== POST / (sample_request.json)"
curl.exe -s -X POST http://127.0.0.1:18081/ `
    -H "Content-Type: application/json" `
    --data-binary "@$PSScriptRoot/sample_request.json"
Write-Host "`n== container logs"
docker logs xgb-0817-test
Write-Host "`n(stop with: docker rm -f xgb-0817-test)"
