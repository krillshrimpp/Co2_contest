$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { $pythonPath = (Get-Command python -ErrorAction Stop).Source }
$tunnelPath = Join-Path $PSScriptRoot '.local\cloudflared.exe'
$statePath = Join-Path $PSScriptRoot '.local\sharing.json'
if (-not (Test-Path -LiteralPath $tunnelPath)) { throw '공식 cloudflared.exe를 .local 폴더에 설치하세요.' }
if (Test-Path -LiteralPath $statePath) { throw 'stop-sharing.ps1 실행 후 다시 시작하세요.' }
if (Get-NetTCPConnection -LocalPort 8767 -State Listen -ErrorAction SilentlyContinue) { throw '8767 포트가 사용 중입니다.' }
$tunnelLog = Join-Path $PSScriptRoot 'outputs\tunnel-error.log'
$tunnelProcess = Start-Process -FilePath $tunnelPath -ArgumentList 'tunnel','--no-autoupdate','--protocol','http2','--url','http://127.0.0.1:8767' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PSScriptRoot 'outputs\tunnel.log') -RedirectStandardError $tunnelLog -PassThru
try {
    $publicUrl = $null
    for ($attempt = 0; $attempt -lt 45; $attempt++) {
        if ($tunnelProcess.HasExited) { throw '터널 시작 실패. outputs/tunnel-error.log를 확인하세요.' }
        if (Test-Path -LiteralPath $tunnelLog) {
            $logText = Get-Content -LiteralPath $tunnelLog -Raw
            if (-not [string]::IsNullOrEmpty($logText)) {
                $match = [regex]::Match($logText, 'https://[a-z0-9-]+\.trycloudflare\.com')
                if ($match.Success) { $publicUrl = $match.Value; break }
            }
        }
        Start-Sleep -Seconds 1
    }
    if (-not $publicUrl) { throw '공유 주소 생성 시간 초과' }
    $serverScript = Join-Path $PSScriptRoot 'public_server.py'
    $publicProcess = Start-Process -FilePath $pythonPath -ArgumentList ('"' + $serverScript + '"'),'--port','8767','--origin',$publicUrl -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PSScriptRoot 'outputs\public-server.log') -RedirectStandardError (Join-Path $PSScriptRoot 'outputs\public-server-error.log') -PassThru
    @{url=$publicUrl; tunnel_pid=$tunnelProcess.Id; server_pid=$publicProcess.Id} | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8
    Write-Output $publicUrl
} catch {
    if (-not $tunnelProcess.HasExited) { Stop-Process -Id $tunnelProcess.Id }
    throw
}
