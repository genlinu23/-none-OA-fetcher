$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$PythonCandidates = @(
  "C:\Users\logan\AppData\Local\Programs\Python\Python312\python.exe",
  "C:\Users\logan\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
  "python"
)
$PythonExe = $PythonCandidates | Where-Object { $_ -eq "python" -or (Test-Path -LiteralPath $_) } | Select-Object -First 1
$ScriptPath = Join-Path $PSScriptRoot "launch_ligen_web.py"
$LogDir = Join-Path $Root "logs"
$StdoutLog = Join-Path $LogDir "launch_ligen_web_stdout.log"
$StderrLog = Join-Path $LogDir "launch_ligen_web_stderr.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Get-LigenUrl {
  param([int]$Port)
  return ("http://127.0.0.1:" + $Port + "/")
}

function Test-LigenSite {
  param([int]$Port)
  try {
    $targetUrl = Get-LigenUrl -Port $Port
    $response = Invoke-WebRequest -Uri $targetUrl -UseBasicParsing -TimeoutSec 2
    return ($response.StatusCode -eq 200 -and $response.Content.Contains('type="module"'))
  } catch {
    return $false
  }
}

function Start-LigenSite {
  param([int]$Port)
  $env:LIGEN_AGENT_MODEL = "gpt-5.4-mini"
  $env:LIGEN_AGENT_REQUEST_TIMEOUT_SECONDS = "18"
  $env:LIGEN_AGENT_RETRY_ATTEMPTS = "2"
  $env:LIGEN_AGENT_RETRY_BASE_DELAY_SECONDS = "0.4"
  Start-Process `
    -FilePath $PythonExe `
    -ArgumentList @("`"$ScriptPath`"", "--host", "127.0.0.1", "--port", "$Port") `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $StdoutLog `
    -RedirectStandardError $StderrLog | Out-Null
}

function Wait-LigenSite {
  param([int]$Port, [int]$Seconds = 20)
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    if (Test-LigenSite -Port $Port) {
      return $true
    }
    Start-Sleep -Milliseconds 500
  }
  return $false
}

function Open-LigenSite {
  param([int]$Port)
  $url = Get-LigenUrl -Port $Port
  $browserCandidates = @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
  )
  $browser = $browserCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
  if ($browser) {
    Start-Process -FilePath $browser -ArgumentList @("--app=$url")
  } else {
    Start-Process $url
  }
}

$selectedPort = $null
foreach ($port in @(8765, 8766, 8767)) {
  if (Test-LigenSite -Port $port) {
    $selectedPort = $port
    break
  }
}

if ($null -eq $selectedPort) {
  foreach ($port in @(8765, 8766, 8767)) {
    Start-LigenSite -Port $port
    if (Wait-LigenSite -Port $port -Seconds 12) {
      $selectedPort = $port
      break
    }
  }
}

if ($null -eq $selectedPort) {
  $message = "Ligen Local Web failed to start. Check ports 8765-8767 or log: " + $StderrLog
  Add-Type -AssemblyName PresentationFramework
  [System.Windows.MessageBox]::Show($message, "Ligen Local Web") | Out-Null
  exit 1
}

Open-LigenSite -Port $selectedPort
