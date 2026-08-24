<#
    Registers savi-discord-notifier as a Windows Scheduled Task that starts at logon
    and runs silently in the background (no console window).

    Run from this folder:
        powershell -ExecutionPolicy Bypass -File .\install-windows.ps1

    Remove it again with:
        powershell -ExecutionPolicy Bypass -File .\install-windows.ps1 -Uninstall
#>

param(
    [switch]$Uninstall,
    [string]$TaskName = "SaviDiscordNotifier"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "savi_notify.py"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName' found."
    }
    return
}

if (-not (Test-Path $script)) {
    throw "Could not find savi_notify.py next to this script."
}
if (-not (Test-Path (Join-Path $here "config.json"))) {
    throw "No config.json yet. Copy config.example.json to config.json and fill it in first."
}

# pythonw.exe runs without popping up a console window.
$python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $python) {
    $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    Write-Warning "pythonw.exe not found - using python.exe, which will show a console window."
}
if (-not $python) { throw "Python is not on your PATH." }

$action    = New-ScheduledTaskAction -Execute $python -Argument "`"$script`"" -WorkingDirectory $here
$trigger   = New-ScheduledTaskTrigger -AtLogOn
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                                          -DontStopIfGoingOnBatteries `
                                          -StartWhenAvailable `
                                          -RestartInterval (New-TimeSpan -Minutes 5) `
                                          -RestartCount 3

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName `
                       -Action $action `
                       -Trigger $trigger `
                       -Settings $settings `
                       -Description "Pings Discord when Savi finishes a task on spawn.co." | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "Installed and started '$TaskName'." -ForegroundColor Green
Write-Host "It will now start automatically every time you log in."
Write-Host "Stop it with:  Stop-ScheduledTask -TaskName $TaskName"
Write-Host "Remove it with: .\install-windows.ps1 -Uninstall"
