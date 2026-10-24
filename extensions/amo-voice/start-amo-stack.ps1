param([ValidateSet('start','status')][string]$Action = 'start')

$ErrorActionPreference = 'Continue'
$logDir = 'E:\DSH\AmoData\logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$log = Join-Path $logDir 'amo-stack.log'

function Write-Log([string]$message) {
    $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message
    Add-Content -LiteralPath $log -Value $line -Encoding UTF8
}

function Test-Port([int]$port) {
    try {
        return [bool](Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction Stop | Select-Object -First 1)
    } catch { return $false }
}

function Wait-Port([int]$port, [int]$seconds = 90) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Port $port) { return $true }
        Start-Sleep -Seconds 1
    }
    return (Test-Port $port)
}

function Start-Hidden([string]$file, [string[]]$arguments, [string]$workingDirectory, [string]$name) {
    $out = Join-Path $logDir "$name.out.log"
    $err = Join-Path $logDir "$name.err.log"
    $process = Start-Process -FilePath $file -ArgumentList $arguments -WorkingDirectory $workingDirectory `
        -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru
    Write-Log "$name started pid=$($process.Id)"
    return $process
}

$gptDir = 'E:\GPT So\GPT-SoVITS-v2pro-20250604\GPT-SoVITS-v2pro-20250604'
$voiceDir = 'E:\Weixin\data\workspace\state\voice'
$rvcDir = 'E:\RVC\app\RVC20260718Nvidia'
$viewer = 'E:\Weixin\data\workspace\scripts\amo-memory-viewer.mjs'
$node = 'E:\Weixin\data\runtimes\0.4.0-db3bce768f85\node.exe'
$gptPython = Join-Path $gptDir 'runtime\python.exe'
$rvcPython = Join-Path $rvcDir 'runtime\python.exe'

if ($Action -eq 'status') {
    foreach ($port in 9880, 8790, 8898, 8899) {
        Write-Host ("{0} = {1}" -f $port, (Test-Port $port))
    }
    exit 0
}

Write-Log 'stack start requested'

if (-not (Test-Port 9880)) {
    if (Test-Path $gptPython) {
        Start-Hidden $gptPython @('api_v2.py','-a','127.0.0.1','-p','9880','-c','GPT_SoVITS/configs/tts_infer.yaml') $gptDir 'gpt-sovits-api' | Out-Null
        if (-not (Wait-Port 9880 120)) { Write-Log 'gpt-sovits-api did not open 9880' }
    } else { Write-Log "missing $gptPython" }
} else { Write-Log 'gpt-sovits-api already running' }

if (-not (Test-Port 8790)) {
    if (Test-Path $gptPython) {
        Start-Hidden $gptPython @('E:\Weixin\data\workspace\state\voice\voice_service.py') $voiceDir 'voice-service' | Out-Null
        if (-not (Wait-Port 8790 30)) { Write-Log 'voice-service did not open 8790' }
    }
} else { Write-Log 'voice-service already running' }

if (-not (Test-Port 8898)) {
    if (Test-Path $rvcPython) {
        Start-Hidden $rvcPython @('E:\Weixin\data\workspace\state\voice\song_service.py') $voiceDir 'singing-service' | Out-Null
        if (-not (Wait-Port 8898 20)) { Write-Log 'singing-service did not open 8898' }
    }
} else { Write-Log 'singing-service already running' }

if (-not (Test-Port 8899) -and (Test-Path $viewer) -and (Test-Path $node)) {
    Start-Hidden $node @($viewer) 'E:\Weixin\data\workspace' 'memory-viewer' | Out-Null
    Wait-Port 8899 10 | Out-Null
} else { Write-Log 'memory-viewer already running or missing' }

Write-Log 'stack start finished'
