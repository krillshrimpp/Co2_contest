$ErrorActionPreference = 'Stop'
$statePath = Join-Path $PSScriptRoot '.local\sharing.json'
if (-not (Test-Path -LiteralPath $statePath)) { Write-Output '공유 실행 정보 없음'; return }
$shareState = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
foreach ($entry in @(@{id=$shareState.tunnel_pid; path=(Join-Path $PSScriptRoot '.local\cloudflared.exe')}, @{id=$shareState.server_pid; path=(Join-Path $PSScriptRoot 'public_server.py')})) {
    $targetProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($entry.id)" -ErrorAction SilentlyContinue
    if ($targetProcess -and $targetProcess.CommandLine.Contains($entry.path)) { Stop-Process -Id $entry.id }
}
Remove-Item -LiteralPath $statePath
Write-Output '외부 공유 종료. 로컬 서버는 유지됩니다.'
