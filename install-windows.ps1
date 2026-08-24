<#
    Sets savi-discord-notifier to start automatically at login and run silently.

    Two ways to do that:
      * A Scheduled Task - preferred, because it restarts the script if it dies.
        Registering one often needs admin, though.
      * A shortcut in your Startup folder - no admin needed, works everywhere.

    This tries the task first and falls back to the shortcut automatically.

        powershell -ExecutionPolicy Bypass -File .\install-windows.ps1

    Force one or the other with -Method task | startup.
    Remove whichever got installed with -Uninstall.
#>

param(
    [switch]$Uninstall,
    [ValidateSet("auto", "task", "startup")]
    [string]$Method = "auto",
    [string]$TaskName = "SaviDiscordNotifier"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "savi_notify.py"
$startupDir = [Environment]::GetFolderPath("Startup")
$lnkPath = Join-Path $startupDir "$TaskName.lnk"


function Stop-Notifier {
    # Kill any pythonw already running our script, so we don't stack copies.
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
                 Where-Object { $_.CommandLine -and $_.CommandLine -like "*savi_notify.py*" }
        foreach ($p in $procs) {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "Stopped a running notifier (PID $($p.ProcessId))."
        }
    } catch { }
}


if ($Uninstall) {
    $removed = $false
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
        $removed = $true
    }
    if (Test-Path $lnkPath) {
        Remove-Item $lnkPath -Force
        Write-Host "Removed startup shortcut."
        $removed = $true
    }
    Stop-Notifier
    if (-not $removed) { Write-Host "Nothing was installed." }
    return
}

# --- checks ---------------------------------------------------------------

if (-not (Test-Path $script)) {
    throw "Could not find savi_notify.py next to this script."
}
if (-not (Test-Path (Join-Path $here "config.json"))) {
    throw "No config.json yet. Copy config.example.json to config.json and fill it in first."
}

# pythonw.exe runs without a console window; python.exe would leave one open.
$python = $null
$cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($cmd) {
    $python = $cmd.Source
} else {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        # pythonw normally sits right next to python.
        $guess = Join-Path (Split-Path -Parent $cmd.Source) "pythonw.exe"
        if (Test-Path $guess) {
            $python = $guess
        } else {
            $python = $cmd.Source
            Write-Warning "pythonw.exe not found - using python.exe, which shows a console window."
        }
    }
}
if (-not $python) { throw "Python is not on your PATH." }

Stop-Notifier

# --- install --------------------------------------------------------------

$installed = ""

if ($Method -eq "auto" -or $Method -eq "task") {
    try {
        $action   = New-ScheduledTaskAction -Execute $python -Argument "`"$script`"" -WorkingDirectory $here
        $trigger  = New-ScheduledTaskTrigger -AtLogOn
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
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
                               -Description "Pings Discord when Savi finishes a task on spawn.co." `
                               -ErrorAction Stop | Out-Null
        $installed = "task"
    } catch {
        if ($Method -eq "task") {
            throw "Could not register the scheduled task: $($_.Exception.Message)"
        }
        Write-Host "Scheduled task needs admin here, using the Startup folder instead." -ForegroundColor Yellow
    }
}

if (-not $installed) {
    if (-not (Test-Path $startupDir)) { throw "Could not find your Startup folder." }

    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($lnkPath)
    $lnk.TargetPath = $python
    $lnk.Arguments = "`"$script`""
    $lnk.WorkingDirectory = $here
    $lnk.WindowStyle = 7          # minimised; pythonw shows nothing anyway
    $lnk.Description = "Pings Discord when Savi finishes a task on spawn.co."
    $lnk.Save()
    $installed = "startup"
}

# Start it now so you don't have to log out and back in.
Start-Process -FilePath $python -ArgumentList "`"$script`"" -WorkingDirectory $here -WindowStyle Hidden

Write-Host ""
if ($installed -eq "task") {
    Write-Host "Installed as a Scheduled Task and started it." -ForegroundColor Green
    Write-Host "Pause with:  Stop-ScheduledTask -TaskName $TaskName"
} else {
    Write-Host "Installed a Startup shortcut and started it." -ForegroundColor Green
    Write-Host "Shortcut: $lnkPath"
}
Write-Host "It will start automatically every time you log in."
Write-Host "Remove it with: .\install-windows.ps1 -Uninstall"
Write-Host ""
Write-Host "Check it's alive:" -ForegroundColor Cyan
Write-Host "  Get-CimInstance Win32_Process -Filter `"Name='pythonw.exe'`" | Where-Object { `$_.CommandLine -like '*savi_notify*' }"
