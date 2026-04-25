#Requires -Version 5.1
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

Add-Type @"
using System;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Runtime.InteropServices;

public class NativeMethods {
    [DllImport("kernel32.dll")] public static extern bool FreeConsole();
    [DllImport("kernel32.dll")] public static extern bool AttachConsole(uint pid);
    [DllImport("kernel32.dll")] public static extern bool GenerateConsoleCtrlEvent(uint ev, uint pg);
    public const uint CTRL_BREAK_EVENT = 1;
}

// Pure-C# event pump. The .NET thread pool fires Output/Error/Exited events
// on background threads; if we attach a PowerShell scriptblock as the handler,
// PS has to re-enter its single-threaded runspace from those threads — which
// in 5.1 deadlocks, drops state, or terminates the host without throwing
// anything catchable. Doing the bridging in compiled C# keeps PS off the
// background threads entirely. The UI-thread drain timer pops items here.
public static class LogPump {
    public const int KIND_OUT  = 0;
    public const int KIND_ERR  = 1;
    public const int KIND_EXIT = 2;

    public static readonly ConcurrentQueue<Tuple<int,string>> Queue
        = new ConcurrentQueue<Tuple<int,string>>();

    public static void Attach(Process p) {
        p.OutputDataReceived += (s, e) => {
            if (e.Data != null) Queue.Enqueue(Tuple.Create(KIND_OUT, e.Data));
        };
        p.ErrorDataReceived += (s, e) => {
            if (e.Data != null) Queue.Enqueue(Tuple.Create(KIND_ERR, e.Data));
        };
        p.Exited += (s, e) => {
            int code = -1;
            try { code = p.ExitCode; } catch { }
            Queue.Enqueue(Tuple.Create(KIND_EXIT, code.ToString()));
        };
    }
}
"@

$VERSION    = "0.4.7"
$repoRoot   = Split-Path $PSScriptRoot -Parent
$DEFAULT_PORT         = 8080
$script:proc          = $null
$script:status        = "Stopped"   # Stopped | Starting | Running | Stopping | Crashed
$script:startTime     = $null
$script:pollCount     = 0
$MAX_POLL_TICKS       = 60          # 30 s at 500 ms
$MAX_LOG_LINES        = 1000
$STOP_TIMEOUT_MS      = 15000

# ── helpers ────────────────────────────────────────────────────────────────────

function Set-Status {
    # Called from UI thread (button clicks, drain timer, poll timer).
    param([string]$s)
    $script:status = $s
    if ($form.IsDisposed) { return }
    $colors = @{
        Stopped  = [System.Drawing.Color]::Gray
        Starting = [System.Drawing.Color]::Gold
        Running  = [System.Drawing.Color]::LimeGreen
        Stopping = [System.Drawing.Color]::Orange
        Crashed  = [System.Drawing.Color]::Red
    }
    $logoBox.Tag         = $colors[$s]
    $logoBox.Invalidate()
    $statusLabel.Text    = $s
    $statusLabel.ForeColor = $colors[$s]

    $btnStart.Text       = if ($s -in "Running","Starting","Stopping") { "Stop" } else { "Start" }
    $btnStart.Enabled    = $s -in "Stopped","Running","Crashed"
}

function Append-Log {
    # Always called from the UI thread (form events, button clicks, drain timer).
    # Background-thread callbacks from the child process push to LogPump.Queue
    # instead, and the drain timer reaches us from the UI thread.
    param([string]$line, [System.Drawing.Color]$color = [System.Drawing.Color]::LightGray)
    if ([string]::IsNullOrEmpty($line)) { return }
    if ($form.IsDisposed) { return }
    $rtb.SelectionStart  = $rtb.TextLength
    $rtb.SelectionLength = 0
    $rtb.SelectionColor  = $color
    $rtb.AppendText("$line`n")

    $lines = $rtb.Lines
    if ($lines.Count -gt $MAX_LOG_LINES) {
        $keep = $lines | Select-Object -Last $MAX_LOG_LINES
        $rtb.Text = $keep -join "`n"
    }
    $rtb.SelectionStart = $rtb.TextLength
    $rtb.ScrollToCaret()
}

function Start-Server {
    Set-Status "Starting"
    $script:pollCount = 0
    $script:startTime = Get-Date
    Append-Log "$(Get-Date -f 'HH:mm:ss')  Starting server on port $DEFAULT_PORT..." ([System.Drawing.Color]::Cyan)

    $cmdArgs = "-m src.main --port $DEFAULT_PORT -v"

    $psi = [System.Diagnostics.ProcessStartInfo]::new("python", $cmdArgs)
    $psi.WorkingDirectory          = $repoRoot
    $psi.UseShellExecute           = $false
    $psi.CreateNoWindow            = $true
    $psi.RedirectStandardOutput    = $true
    $psi.RedirectStandardError     = $true

    $script:proc = [System.Diagnostics.Process]::new()
    $script:proc.StartInfo = $psi
    $script:proc.EnableRaisingEvents = $true

    # Hand the process to the C# pump — Output/Error/Exited events are handled
    # in compiled code that just enqueues. Nothing PS runs on .NET background
    # threads. The drain timer (UI thread) consumes the queue.
    [LogPump]::Attach($script:proc)

    $script:proc.Start()          | Out-Null
    $script:proc.BeginOutputReadLine()
    $script:proc.BeginErrorReadLine()

    $pollTimer.Start()
    $logDrainTimer.Start()
}

function Stop-Server {
    if ($null -eq $script:proc -or $script:proc.HasExited) {
        Set-Status "Stopped"
        return
    }
    Set-Status "Stopping"
    $pollTimer.Stop()
    Append-Log "$(Get-Date -f 'HH:mm:ss')  Sending stop signal..." ([System.Drawing.Color]::Cyan)

    # Send CTRL_BREAK to the process group
    [NativeMethods]::FreeConsole()             | Out-Null
    [NativeMethods]::AttachConsole($script:proc.Id) | Out-Null
    [NativeMethods]::GenerateConsoleCtrlEvent([NativeMethods]::CTRL_BREAK_EVENT, 0) | Out-Null
    [NativeMethods]::FreeConsole()             | Out-Null

    # Force-kill fallback timer
    $killTimer.Interval = $STOP_TIMEOUT_MS
    $killTimer.Start()
}

# ── poll timer: check http ready ───────────────────────────────────────────────
$pollTimer          = [System.Windows.Forms.Timer]::new()
$pollTimer.Interval = 500
$pollTimer.add_Tick({
    # If process died before we got ready
    if ($null -ne $script:proc -and $script:proc.HasExited) {
        $pollTimer.Stop()
        return
    }

    $script:pollCount++
    if ($script:pollCount -gt $MAX_POLL_TICKS) {
        $pollTimer.Stop()
        Append-Log "$(Get-Date -f 'HH:mm:ss')  Startup timed out after 30s." ([System.Drawing.Color]::Red)
        Stop-Server
        return
    }

    try {
        $req = [System.Net.WebRequest]::Create("http://127.0.0.1:$DEFAULT_PORT/")
        $req.Timeout = 400
        $resp = $req.GetResponse()
        $resp.Close()
        $pollTimer.Stop()
        Set-Status "Running"
        Append-Log "$(Get-Date -f 'HH:mm:ss')  Server is ready." ([System.Drawing.Color]::LimeGreen)
        Start-Process "http://localhost:$DEFAULT_PORT/"
    } catch { <# not ready yet #> }
})

# ── kill-fallback timer ────────────────────────────────────────────────────────
$killTimer          = [System.Windows.Forms.Timer]::new()
$killTimer.Interval = $STOP_TIMEOUT_MS
$killTimer.add_Tick({
    $killTimer.Stop()
    if ($null -ne $script:proc -and -not $script:proc.HasExited) {
        Append-Log "$(Get-Date -f 'HH:mm:ss')  Force-killing server after timeout." ([System.Drawing.Color]::Red)
        $script:proc.Kill()
    }
})

# ── log-drain timer ────────────────────────────────────────────────────────────
# Pulls items from the C# LogPump queue on the UI thread and renders them.
$logDrainTimer          = [System.Windows.Forms.Timer]::new()
$logDrainTimer.Interval = 50
$logDrainTimer.add_Tick({
    $item = $null
    while ([LogPump]::Queue.TryDequeue([ref]$item)) {
        switch ($item.Item1) {
            0 { Append-Log $item.Item2 }                                            # stdout
            1 { Append-Log $item.Item2 ([System.Drawing.Color]::Yellow) }           # stderr
            2 {
                # process exit
                if ($script:status -eq "Stopping") {
                    Set-Status "Stopped"
                    Append-Log "$(Get-Date -f 'HH:mm:ss')  Server stopped (exit $($item.Item2))." ([System.Drawing.Color]::Cyan)
                } elseif ($script:status -ne "Stopped") {
                    Set-Status "Crashed"
                    Append-Log "$(Get-Date -f 'HH:mm:ss')  Server process exited unexpectedly (exit $($item.Item2))." ([System.Drawing.Color]::Red)
                }
                $pollTimer.Stop()
                $logDrainTimer.Stop()
            }
        }
    }
})

# ── build form ─────────────────────────────────────────────────────────────────
$form               = [System.Windows.Forms.Form]::new()
$form.Text          = "Aerial Object Detection Launcher  v$VERSION"
$form.Size          = [System.Drawing.Size]::new(720, 540)
$form.MinimumSize   = [System.Drawing.Size]::new(520, 400)
$form.BackColor     = [System.Drawing.Color]::FromArgb(30, 30, 30)
$form.ForeColor     = [System.Drawing.Color]::WhiteSmoke
$form.Font          = [System.Drawing.Font]::new("Segoe UI", 9)

# top panel
$topPanel           = [System.Windows.Forms.Panel]::new()
$topPanel.Dock      = "Top"
$topPanel.Height    = 56
$topPanel.BackColor = [System.Drawing.Color]::FromArgb(40, 40, 40)
$topPanel.Padding   = [System.Windows.Forms.Padding]::new(10, 6, 10, 6)

# Project logo, custom-painted (mirrors src/web/static/img/logo.svg).
# Color shifts with status, so this doubles as the live indicator.
$logoBox             = [System.Windows.Forms.PictureBox]::new()
$logoBox.Size        = [System.Drawing.Size]::new(44, 44)
$logoBox.Location    = [System.Drawing.Point]::new(10, 6)
$logoBox.BackColor   = [System.Drawing.Color]::Transparent
$logoBox.Tag         = [System.Drawing.Color]::Gray   # current status color
$logoBox.add_Paint({
    param($s, $e)
    $g = $e.Graphics
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $col = $s.Tag
    # SVG is authored in a 128x128 viewbox; scale once and draw in those units.
    $g.ScaleTransform($s.Width / 128.0, $s.Height / 128.0)

    $pen3  = [System.Drawing.Pen]::new($col, 3.0)
    $pen25 = [System.Drawing.Pen]::new($col, 2.5)
    $pen2  = [System.Drawing.Pen]::new($col, 2.0)

    # Concentric target rings (r = 60, 46, 32, 18 in 128x128 space)
    $g.DrawEllipse($pen3,   4,  4, 120, 120)
    $g.DrawEllipse($pen25, 18, 18,  92,  92)
    $g.DrawEllipse($pen2,  32, 32,  64,  64)
    $g.DrawEllipse($pen2,  46, 46,  36,  36)

    # Crosshair lines
    $g.DrawLine($pen2, 64,  2, 64, 126)
    $g.DrawLine($pen2,  2, 64, 126,  64)

    # Center object: faint halo, solid dot, white pinpoint
    $halo = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb( 38, $col.R, $col.G, $col.B))
    $core = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(217, $col.R, $col.G, $col.B))
    $pip  = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(242, 255, 255, 255))
    $g.FillEllipse($halo, 59,    59,    10,  10)
    $g.FillEllipse($core, 61,    61,     6,   6)
    $g.FillEllipse($pip,  62.8,  62.8,   2.4, 2.4)

    # Trajectory trail (approaching from lower-left, opacity ramps up)
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
})

$statusLabel        = [System.Windows.Forms.Label]::new()
$statusLabel.Text   = "Stopped"
$statusLabel.Font   = [System.Drawing.Font]::new("Segoe UI", 11)
$statusLabel.ForeColor = [System.Drawing.Color]::Gray
$statusLabel.AutoSize  = $true
$statusLabel.Location  = [System.Drawing.Point]::new(62, 18)

# buttons
$btnStart           = [System.Windows.Forms.Button]::new()
$btnStart.Text      = "Start"
$btnStart.Size      = [System.Drawing.Size]::new(90, 30)
$btnStart.Location  = [System.Drawing.Point]::new(180, 13)
$btnStart.FlatStyle = "Flat"
$btnStart.BackColor = [System.Drawing.Color]::FromArgb(0, 120, 60)
$btnStart.ForeColor = [System.Drawing.Color]::White
$btnStart.FlatAppearance.BorderSize = 0

$topPanel.Controls.AddRange(@($logoBox, $statusLabel, $btnStart))

# log area
$rtb                = [System.Windows.Forms.RichTextBox]::new()
$rtb.Dock           = "Fill"
$rtb.BackColor      = [System.Drawing.Color]::FromArgb(18, 18, 18)
$rtb.ForeColor      = [System.Drawing.Color]::LightGray
$rtb.Font           = [System.Drawing.Font]::new("Consolas", 9)
$rtb.ReadOnly       = $true
$rtb.WordWrap       = $false
$rtb.ScrollBars     = "Both"
$rtb.BorderStyle    = "None"

$form.Controls.AddRange(@($rtb, $topPanel))

# ── wire events ────────────────────────────────────────────────────────────────
$btnStart.add_Click({
    if ($script:status -in "Stopped","Crashed") {
        Start-Server
    } elseif ($script:status -eq "Running") {
        Stop-Server
    }
})

$form.add_FormClosing({
    param($s, $e)
    if ($null -ne $script:proc -and -not $script:proc.HasExited) {
        $ans = [System.Windows.Forms.MessageBox]::Show(
            "The server is running. Stop it and close?",
            "Aerial Object Detection",
            [System.Windows.Forms.MessageBoxButtons]::YesNo,
            [System.Windows.Forms.MessageBoxIcon]::Question)
        if ($ans -eq [System.Windows.Forms.DialogResult]::No) {
            $e.Cancel = $true
            return
        }
        $pollTimer.Stop()
        $killTimer.Stop()
        # Graceful stop then wait
        [NativeMethods]::FreeConsole()                  | Out-Null
        [NativeMethods]::AttachConsole($script:proc.Id) | Out-Null
        [NativeMethods]::GenerateConsoleCtrlEvent([NativeMethods]::CTRL_BREAK_EVENT, 0) | Out-Null
        [NativeMethods]::FreeConsole()                  | Out-Null
        $script:proc.WaitForExit($STOP_TIMEOUT_MS) | Out-Null
        if (-not $script:proc.HasExited) { $script:proc.Kill() }
    }
    $pollTimer.Dispose()
    $killTimer.Dispose()
})

$form.add_Shown({
    Append-Log "Aerial Object Detection Launcher v$VERSION" ([System.Drawing.Color]::Cyan)
    Append-Log "Repo: $repoRoot" ([System.Drawing.Color]::DarkGray)
    Append-Log "Click Start to launch the server." ([System.Drawing.Color]::DarkGray)
})

# ── error logging ──────────────────────────────────────────────────────────────
$script:crashLog = Join-Path $repoRoot "launcher_crash.log"

# Catch exceptions thrown on the UI thread (BeginInvoke callbacks, timer ticks, etc.)
[System.Windows.Forms.Application]::add_ThreadException({
    param($s, $e)
    $msg = "[$(Get-Date -f 'HH:mm:ss')] ThreadException: $($e.Exception.GetType().FullName): $($e.Exception.Message)`n$($e.Exception.StackTrace)`n"
    $msg | Out-File $script:crashLog -Append -Encoding UTF8
    [System.Windows.Forms.MessageBox]::Show("Launcher error (see launcher_crash.log):`n$($e.Exception.Message)", "Launcher Error",
        [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
})

# Catch unhandled exceptions on background threads
[System.AppDomain]::CurrentDomain.add_UnhandledException({
    param($s, $e)
    $msg = "[$(Get-Date -f 'HH:mm:ss')] UnhandledException: $($e.ExceptionObject.GetType().FullName): $($e.ExceptionObject.Message)`n$($e.ExceptionObject.StackTrace)`n"
    $msg | Out-File $script:crashLog -Append -Encoding UTF8
})

# ── run ────────────────────────────────────────────────────────────────────────
[System.Windows.Forms.Application]::EnableVisualStyles()
try {
    [System.Windows.Forms.Application]::Run($form)
} catch {
    $msg = "[$(Get-Date -f 'HH:mm:ss')] Application.Run exception: $($_.Exception.GetType().FullName): $($_.Exception.Message)`n$($_.Exception.StackTrace)`n"
    $msg | Out-File $script:crashLog -Append -Encoding UTF8
    throw
}
