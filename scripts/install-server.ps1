# Runs the JobApps server on this PC whenever you're logged in, and makes the dashboard reachable
# from your other devices over Tailscale (tailnet only - NOT Funnel/public).
#   powershell -ExecutionPolicy Bypass -File scripts\install-server.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install-server.ps1 -Remove
param([switch]$Remove)

$TaskName = "JobApps Server"
$Port = 8765
$HttpsPort = 8443
$Tailscale = "C:\Program Files\Tailscale\tailscale.exe"

if ($Remove) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    & $Tailscale serve --https=$HttpsPort off
    Write-Host "Removed the JobApps server task and its Tailscale serve rule."
    return
}

$Root = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Pythonw)) { throw "Virtualenv not found at $Pythonw - see README > Setup." }

$Action = New-ScheduledTaskAction -Execute $Pythonw -Argument "-m jobapps serve" -WorkingDirectory $Root
$AtLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Watchdog: fires every 5 minutes forever. With MultipleInstances IgnoreNew it does nothing while
# the server is up, and relaunches it within 5 minutes if it ever exits.
$Watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger @($AtLogon, $Watchdog) -Settings $Settings `
    -Description "JobApps dashboard + daily job picks (http://127.0.0.1:$Port). Relaunched by a 5-minute watchdog." -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

# Tailnet-only HTTPS on port $HttpsPort -> the local server. Other serve/funnel ports are left as they are.
& $Tailscale serve --bg --https=$HttpsPort "http://127.0.0.1:$Port"
$Dns = ((& $Tailscale status --json | ConvertFrom-Json).Self.DNSName).TrimEnd('.')
Write-Host ""
Write-Host "JobApps server installed. Open on your Mac: https://$Dns`:$HttpsPort"
Write-Host "Log: $Root\data\jobapps.log"
