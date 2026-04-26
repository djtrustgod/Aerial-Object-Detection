#Requires -Version 5.1
<#
.SYNOPSIS
    Creates a Desktop shortcut that launches the Aerial Object Detection
    server GUI, with the project logo as its icon.

.DESCRIPTION
    Windows 11 will not let you pin a .bat file to the taskbar, but it
    will pin a powershell.exe shortcut. This installer:

      1. Renders the project logo to an .ico file (cached in $env:APPDATA
         so the repo stays clean of binary artifacts).
      2. Creates a Desktop shortcut whose target is powershell.exe with
         tools\Launch-AerialDetect.ps1 as -File. -WindowStyle Hidden
         suppresses the console so only the launcher GUI appears.

    After running this once, right-click the Desktop shortcut ->
    "Show more options" -> "Pin to taskbar".

.NOTES
    Run via:
      powershell -NoProfile -ExecutionPolicy Bypass -File tools\Install-DesktopShortcut.ps1
    Re-running is safe; the shortcut and icon are overwritten.
#>

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$repoRoot     = Split-Path $PSScriptRoot -Parent
$ps1Path      = Join-Path $PSScriptRoot 'Launch-AerialDetect.ps1'
$shortcutName = 'Aerial Object Detection.lnk'
$shortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) $shortcutName
$iconDir      = Join-Path $env:APPDATA 'AerialObjectDetection'
$iconPath     = Join-Path $iconDir 'aerial-detect.ico'
$psExe        = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'

if (-not (Test-Path $ps1Path)) { throw "Launcher script not found: $ps1Path" }
if (-not (Test-Path $psExe))   { throw "powershell.exe not found at expected path: $psExe" }

# ── 1. Render the logo to an .ico ──────────────────────────────────────────────
# Same shapes the launcher paints (mirrors src/web/static/img/logo.svg).
# 256x256 lets Windows downscale cleanly for taskbar/Start sizes.
New-Item -ItemType Directory -Path $iconDir -Force | Out-Null

$bmp = [System.Drawing.Bitmap]::new(256, 256, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
$g   = [System.Drawing.Graphics]::FromImage($bmp)
$g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$g.Clear([System.Drawing.Color]::Transparent)

$col = [System.Drawing.Color]::FromArgb(34, 197, 94)   # #22c55e from logo.svg
$g.ScaleTransform(256.0 / 128.0, 256.0 / 128.0)         # SVG viewbox is 128

$pen3  = [System.Drawing.Pen]::new($col, 3.0)
$pen25 = [System.Drawing.Pen]::new($col, 2.5)
$pen2  = [System.Drawing.Pen]::new($col, 2.0)

# Concentric rings + crosshairs
$g.DrawEllipse($pen3,   4,  4, 120, 120)
$g.DrawEllipse($pen25, 18, 18,  92,  92)
$g.DrawEllipse($pen2,  32, 32,  64,  64)
$g.DrawEllipse($pen2,  46, 46,  36,  36)
$g.DrawLine($pen2, 64,  2, 64, 126)
$g.DrawLine($pen2,  2, 64, 126,  64)

# Center object: faint halo, solid dot, white pinpoint
$halo = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb( 38, $col.R, $col.G, $col.B))
$core = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(217, $col.R, $col.G, $col.B))
$pip  = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(242, 255, 255, 255))
$g.FillEllipse($halo, 59,    59,    10,  10)
$g.FillEllipse($core, 61,    61,     6,   6)
$g.FillEllipse($pip,  62.8,  62.8,   2.4, 2.4)

# Trajectory trail (lower-left, opacity ramps up)
$t1 = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb( 51, $col.R, $col.G, $col.B))
$t2 = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb( 76, $col.R, $col.G, $col.B))
$t3 = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(115, $col.R, $col.G, $col.B))
$t4 = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(153, $col.R, $col.G, $col.B))
$g.FillEllipse($t1, 40.5, 84.5, 3.0, 3.0)
$g.FillEllipse($t2, 46.5, 78.5, 3.0, 3.0)
$g.FillEllipse($t3, 52.2, 72.2, 3.6, 3.6)
$g.FillEllipse($t4, 57.0, 67.0, 4.0, 4.0)

$pen3.Dispose(); $pen25.Dispose(); $pen2.Dispose()
$halo.Dispose(); $core.Dispose(); $pip.Dispose()
$t1.Dispose(); $t2.Dispose(); $t3.Dispose(); $t4.Dispose()
$g.Dispose()

# Bitmap -> Icon -> file. GetHicon allocates a native handle; release it
# afterwards or it leaks until the process exits.
$hicon = $bmp.GetHicon()
$icon  = [System.Drawing.Icon]::FromHandle($hicon)
$fs    = [System.IO.File]::Open($iconPath, [System.IO.FileMode]::Create)
$icon.Save($fs)
$fs.Close()
$icon.Dispose()
$bmp.Dispose()

Add-Type -Name IconCleaner -Namespace AerialDetect -MemberDefinition @'
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    public static extern bool DestroyIcon(System.IntPtr hIcon);
'@
[AerialDetect.IconCleaner]::DestroyIcon($hicon) | Out-Null

# ── 2. Create the Desktop shortcut ─────────────────────────────────────────────
$shell = New-Object -ComObject WScript.Shell
$lnk   = $shell.CreateShortcut($shortcutPath)
$lnk.TargetPath       = $psExe
$lnk.Arguments        = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ps1Path`""
$lnk.WorkingDirectory = $repoRoot
$lnk.IconLocation     = "$iconPath,0"
$lnk.Description      = 'Aerial Object Detection Launcher'
$lnk.Save()

Write-Host ""
Write-Host "Created shortcut: $shortcutPath" -ForegroundColor Green
Write-Host "Icon:             $iconPath"
Write-Host ""
Write-Host "To pin to the taskbar:" -ForegroundColor Cyan
Write-Host "  Right-click the Desktop shortcut -> ""Show more options"" -> ""Pin to taskbar"""
Write-Host ""
