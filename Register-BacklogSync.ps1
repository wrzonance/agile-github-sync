<#
.SYNOPSIS
  Register (or remove) a Windows Scheduled Task that runs the GitHub -> AgilePlace sync every 30 minutes
  on weekdays, only while you are logged on (no stored password).

.DESCRIPTION
  Idempotent (re-registers on re-run). The task runs `python sync.py --apply` from this folder and
  appends output to sync.log. Prereqs for the run-as user: `python` and `gh` on PATH, `gh auth login`
  done, and .env filled (TARGET_REPO_PATH + AGILEPLACE_*).

  First run may require unblocking:  Unblock-File .\Register-BacklogSync.ps1
  (or invoke as:  powershell -ExecutionPolicy Bypass -File .\Register-BacklogSync.ps1)

.EXAMPLE
  .\Register-BacklogSync.ps1
  .\Register-BacklogSync.ps1 -IntervalMinutes 15 -StartTime 08:00 -ActiveHours 10
  .\Register-BacklogSync.ps1 -Unregister
#>
[CmdletBinding()]
param(
  [string]$TaskName = "CableTool-BacklogSync",
  [int]$IntervalMinutes = 30,
  [string]$StartTime = "07:00",
  [int]$ActiveHours = 12,
  [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Unregister) {
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
  } else {
    Write-Host "No scheduled task '$TaskName' to remove."
  }
  return
}

# Preconditions -- report, don't silently fail.
foreach ($tool in @("python", "gh")) {
  if (-not (Get-Command $tool -CommandType Application -ErrorAction SilentlyContinue)) {
    Write-Warning "'$tool' is not on PATH as an executable -- the scheduled task uses -NoProfile and will fail until it is."
  }
}
if ($IntervalMinutes -le 0 -or $ActiveHours -le 0) { throw "IntervalMinutes and ActiveHours must be positive." }
if (-not (Test-Path (Join-Path $here ".env"))) {
  Write-Warning "No .env in $here -- copy .env.example to .env and fill TARGET_REPO_PATH + AGILEPLACE_*."
}

$log = Join-Path $here "sync.log"
# Run in $here via -WorkingDirectory (a real parameter, so paths with apostrophes/spaces are safe) and
# log to a RELATIVE path -- the -Command string is fully static, so no interpolated path can break it.
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument '-NoProfile -NonInteractive -WindowStyle Hidden -Command "python sync.py --apply *>> sync.log"' `
  -WorkingDirectory $here

# Microsoft's documented -Weekly parameter set has no repetition parameters. Copying .Repetition
# from a -Once trigger is an undocumented community workaround and is load-bearing: do not replace
# it by passing -RepetitionInterval/-RepetitionDuration directly to -Weekly.
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $StartTime
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $StartTime `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Hours $ActiveHours)).Repetition

# Run only when logged on (interactive), no stored password, standard privileges.
$principal = New-ScheduledTaskPrincipal `
  -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
  -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Registered '$TaskName': every $IntervalMinutes min, weekdays from $StartTime for $ActiveHours h, only while logged on."
Write-Host "Logs: $log"
Write-Host "Remove with: .\Register-BacklogSync.ps1 -Unregister"
