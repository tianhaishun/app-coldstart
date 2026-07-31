# CI 构建脚本 —— 在 GitLab Runner (Windows) 上执行
# 由 .gitlab-ci.yml 调用，所有复杂逻辑放这里避免 YAML 转义问题

$ErrorActionPreference = "Stop"
$version = $env:CI_COMMIT_TAG.TrimStart("v")
Write-Host "=== Building version $version ==="

# 1. npm install
Write-Host "`n--- npm install ---"
npm install --prefer-offline

# 2. Copy pre-placed deps
Write-Host "`n--- Copy deps ---"
$depsDir = $env:CI_DEPS_DIR
if (-not $depsDir) { $depsDir = "C:\ci-deps\app-coldstart" }
if (-not (Test-Path "scrcpy") -and (Test-Path "$depsDir\scrcpy")) {
    Copy-Item -Recurse -Force "$depsDir\scrcpy" "scrcpy"
}
if (-not (Test-Path "ios") -and (Test-Path "$depsDir\ios")) {
    Copy-Item -Recurse -Force "$depsDir\ios" "ios"
}

# 3. Build python-embed
Write-Host "`n--- Build python-embed ---"
python scripts/build-python-embed.py

# 4. Build installer
Write-Host "`n--- electron-builder ---"
npm run build:win

# 5. Generate docs + ZIP
Write-Host "`n--- Generate docs ---"
python scripts/gen-docs.py
python scripts/make-release-zip.py

# 6. Upload to Package Registry
Write-Host "`n--- Upload artifacts ---"
$base = "$env:CI_API_V4_URL/projects/$env:CI_PROJECT_ID/packages/generic/app-coldstart/$version"

$setup = (Get-ChildItem "release\*-setup.exe" | Select-Object -First 1).FullName
curl.exe --header "JOB-TOKEN: $env:CI_JOB_TOKEN" --upload-file $setup "$base/AppColdStart-setup.exe"

$zip = (Get-ChildItem "publish\*.zip" | Select-Object -First 1).FullName
curl.exe --header "JOB-TOKEN: $env:CI_JOB_TOKEN" --upload-file $zip "$base/AppColdStart-$version.zip"

Write-Host "`n=== Done ==="
