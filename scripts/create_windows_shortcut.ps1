param(
  [string]$ShortcutName = "Meeting Notes Capture",
  [string]$ProjectPath = (Split-Path -Parent $PSScriptRoot)
)

$python = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if (-not $python) {
  $python = Get-Command python.exe -ErrorAction Stop
}

$scriptPath = Join-Path $ProjectPath "scripts\windows_capture_ui.py"
$shell = New-Object -ComObject WScript.Shell
$desktops = @([Environment]::GetFolderPath("Desktop"))
if ($env:OneDrive) {
  $oneDriveDesktop = Join-Path $env:OneDrive "Desktop"
  if (Test-Path -LiteralPath $oneDriveDesktop) {
    $desktops += $oneDriveDesktop
  }
}

foreach ($desktop in ($desktops | Select-Object -Unique)) {
  $shortcutPath = Join-Path $desktop "$ShortcutName.lnk"
  $shortcut = $shell.CreateShortcut($shortcutPath)
  $shortcut.TargetPath = $python.Source
  $shortcut.Arguments = "`"$scriptPath`""
  $shortcut.WorkingDirectory = $ProjectPath
  $shortcut.Description = "Open the Meeting Notes Capture desktop recorder"
  $shortcut.IconLocation = "$env:SystemRoot\System32\SndVol.exe,0"
  $shortcut.Save()
  Write-Output $shortcutPath
}
