<#
  Vesper installer for Windows 10 and 11. Open PowerShell (Start > type PowerShell > Enter, no need for
  administrator) and paste:

      irm https://raw.githubusercontent.com/jwill736/PC-assistant/main/install.ps1 | iex

  What it does: checks Windows lets desktop apps use the microphone; installs Python 3.12 if there's no
  usable one (from python.org, signature checked, just for you, no admin); downloads Vesper into
  C:\Users\<you>\Vesper; installs its packages (setup.bat); asks what to call you, your main goal and
  where your Llama runs (this PC, another PC on your network, Claude, or later); adds Vesper to the
  desktop and Start menu; starts it.

  Run the same line again, or "Update Vesper" in the Start menu, to update. config.yaml, .env and data\
  are never touched by an update.

  Options, e.g.  & ([scriptblock]::Create((irm <the url above>))) -Dir D:\Vesper
    -Dir <folder>      where Vesper goes (default: your user folder\Vesper)
    -Ref <name>        which branch or commit to install (default: main)
    -InstallPython     install Python 3.12.10 even if a usable Python is found
    -Reconfigure       ask the questions again when updating
    -NoStart           don't start Vesper at the end
    -Unattended        ask nothing; -Name, -Goal and -BrainUrl give the answers
#>
param(
    [string]$Dir = (Join-Path $HOME 'Vesper'),
    [string]$Ref = 'main',
    [switch]$InstallPython,
    [switch]$Reconfigure,
    [switch]$NoStart,
    [switch]$Unattended,
    [string]$Name,
    [string]$Goal,
    [string]$BrainUrl
)

$VesperRepo = 'jwill736/PC-assistant'
$VesperPythonVersion = '3.12.10'   # the last 3.12 with a Windows installer; what Vesper is tested on
# One spelling of the folder: "Update Vesper" passes C:\Users\<you>\Vesper\. and a relative -Dir is allowed
$Dir = [System.IO.Path]::GetFullPath($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Dir)).TrimEnd('\')

function Write-Step([string]$Text) { Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan }
function Write-Note([string]$Text) { Write-Host "   $Text" }
function Write-Warn([string]$Text) { Write-Host "   $Text" -ForegroundColor Yellow }

function Get-PythonInfo([string]$Exe, [string[]]$Pre = @()) {
    # A Python that can run Vesper's packages: 3.11 or 3.12, 64-bit x86 build. Returns its path, or $null.
    try {
        $out = & $Exe @Pre -c "import sys, sysconfig; print('%d.%d|%s|%s' % (sys.version_info[0], sys.version_info[1], sysconfig.get_platform(), sys.executable))" 2>$null
    } catch { return $null }
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    $parts = ("$out".Trim()) -split '\|', 3
    if ($parts.Count -eq 3 -and ($parts[0] -eq '3.12' -or $parts[0] -eq '3.11') -and $parts[1] -eq 'win-amd64') {
        return [pscustomobject]@{ Version = $parts[0]; Path = $parts[2] }
    }
    return $null
}

function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in '-3.12', '-3.11') {
            $found = Get-PythonInfo 'py' @($v)
            if ($found) { return $found }
        }
    }
    $paths = @()
    foreach ($v in '3.12', '3.11') {
        foreach ($hive in 'HKCU:', 'HKLM:') {
            $key = "$hive\Software\Python\PythonCore\$v\InstallPath"
            $item = Get-ItemProperty -Path $key -ErrorAction SilentlyContinue
            if ($item) {
                if ($item.ExecutablePath) { $paths += $item.ExecutablePath }
                if ($item.'(default)') { $paths += (Join-Path $item.'(default)' 'python.exe') }
            }
        }
        $short = $v -replace '\.', ''
        $paths += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python$short\python.exe")
        $paths += (Join-Path $env:ProgramFiles "Python$short\python.exe")
    }
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath -and $onPath.Source -notlike '*\WindowsApps\*') { $paths += $onPath.Source }  # not the Store stub
    foreach ($p in $paths) {
        if ($p -and (Test-Path $p)) {
            $found = Get-PythonInfo $p
            if ($found) { return $found }
        }
    }
    return $null
}

function Install-PythonFromPythonOrg {
    $url = "https://www.python.org/ftp/python/$VesperPythonVersion/python-$VesperPythonVersion-amd64.exe"
    $exe = Join-Path $env:TEMP "python-$VesperPythonVersion-amd64.exe"
    Write-Note "Downloading Python $VesperPythonVersion from python.org (about 25 MB)..."
    Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
    $sig = Get-AuthenticodeSignature -FilePath $exe
    if ($sig.Status -ne 'Valid' -or "$($sig.SignerCertificate.Subject)" -notmatch 'Python Software Foundation') {
        Remove-Item $exe -Force -ErrorAction SilentlyContinue
        throw "the Python installer's signature didn't check out ($($sig.Status)), so it wasn't run"
    }
    Write-Note 'Signed by the Python Software Foundation. Installing it just for you (no admin needed)...'
    $proc = Start-Process -FilePath $exe -Wait -PassThru -ArgumentList @(
        '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1', 'InstallLauncherAllUsers=0',
        'Include_test=0', 'Include_doc=0')
    Remove-Item $exe -Force -ErrorAction SilentlyContinue
    if ($proc.ExitCode -ne 0 -and $proc.ExitCode -ne 3010) {
        throw "the Python installer stopped with code $($proc.ExitCode)"
    }
    # This window started before Python was on PATH.
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
}

function Test-MicrophoneAllowed {
    # The Settings switches "Microphone access", "Let apps access your microphone" and
    # "Let desktop apps access your microphone". Off = Vesper runs but hears nothing, and nothing says why.
    $base = 'Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone'
    foreach ($key in "HKLM:\$base", "HKCU:\$base", "HKCU:\$base\NonPackaged") {
        $value = (Get-ItemProperty -Path $key -Name Value -ErrorAction SilentlyContinue).Value
        if ($value -eq 'Deny') { return $false }
    }
    return $true
}

function Test-VesperFolder([string]$Path) {
    return (Test-Path (Join-Path $Path 'assistant\__init__.py')) -and (Test-Path (Join-Path $Path 'start.bat'))
}

function Get-VesperProcesses {
    # Vesper started from this folder: the .venv python.exe (a small launcher), the Python it started, and the
    # start.bat log window around them, which stays open when Vesper didn't exit cleanly.
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $found = @{}
    foreach ($p in $all) {
        $cmd = [string]$p.CommandLine
        if ($cmd -match '-m\s+assistant(\s|"|$)' -and $cmd -notmatch '--quit' -and $p.ExecutablePath -and
            $p.ExecutablePath.StartsWith("$Dir\", [StringComparison]::OrdinalIgnoreCase)) { $found[$p.ProcessId] = $p }
    }
    foreach ($p in $all) {
        $cmd = [string]$p.CommandLine
        if ($found.ContainsKey($p.ParentProcessId) -and $cmd -match '-m\s+assistant(\s|"|$)') { $found[$p.ProcessId] = $p }
    }
    $parents = @($found.Values | ForEach-Object { $_.ParentProcessId })
    foreach ($p in $all) {
        $cmd = ([string]$p.CommandLine).Replace('\.\', '\')
        if ($p.Name -eq 'cmd.exe' -and $cmd -match 'start\.bat' -and ($parents -contains $p.ProcessId -or
            $cmd.IndexOf("$Dir\start.bat", [StringComparison]::OrdinalIgnoreCase) -ge 0)) { $found[$p.ProcessId] = $p }
    }
    return @($found.Values)
}

function Stop-RunningVesper([string]$VenvPython) {
    if (Test-Path $VenvPython) {
        Push-Location $Dir
        try {
            & $VenvPython -m assistant --quit | Out-Null   # closes a running copy so its files can be replaced
        } catch { } finally { Pop-Location }
    }
    # Older copies' --quit only waited for the HUD to close, so a copy that hung on after it kept running (and its
    # log window open) beside the new one. Give what's left 10 seconds, then end it and close its window.
    $left = @(Get-VesperProcesses)
    if ($left.Count) {
        $ids = @($left | Where-Object { $_.Name -ne 'cmd.exe' } | ForEach-Object { $_.ProcessId })
        for ($i = 0; $i -lt 20 -and $ids.Count -and @(Get-Process -Id $ids -ErrorAction SilentlyContinue).Count; $i++) {
            Start-Sleep -Milliseconds 500
        }
        foreach ($p in $left) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 1
        Write-Note 'Closed the Vesper that was running.'
    }
    if (@(Get-VesperProcesses | Where-Object { $_.Name -ne 'cmd.exe' }).Count) {
        Write-Warn 'If Vesper is running, quit it now: right-click the ring icon by the clock > Quit.'
        if (-not $Unattended) { Read-Host '   Press Enter when it is closed' | Out-Null }
    }
}

function Get-VesperCode {
    if (Test-Path (Join-Path $Dir '.git')) {
        if (Get-Command git -ErrorAction SilentlyContinue) {
            Write-Note "Updating the git copy in $Dir..."
            & git -C $Dir pull --ff-only
            if ($LASTEXITCODE -ne 0) { throw "git pull failed in $Dir; commit or undo your changes there, then try again" }
        } else {
            Write-Warn 'This folder is a GitHub Desktop copy: update it there (Fetch origin > Pull origin).'
            Write-Warn 'Continuing with the code it has.'
        }
        return
    }
    $tag = [guid]::NewGuid().ToString('N')
    $zip = Join-Path $env:TEMP "vesper-$tag.zip"
    $unpacked = Join-Path $env:TEMP "vesper-$tag"
    try {
        Write-Note "Downloading Vesper ($Ref) from GitHub..."
        Invoke-WebRequest -Uri "https://github.com/$VesperRepo/archive/$Ref.zip" -OutFile $zip -UseBasicParsing
        Expand-Archive -Path $zip -DestinationPath $unpacked -Force
        $src = Get-ChildItem -Path $unpacked -Directory | Select-Object -First 1
        if (-not $src -or -not (Test-VesperFolder $src.FullName)) { throw "the download didn't contain Vesper" }
        New-Item -ItemType Directory -Force -Path $Dir | Out-Null
        # Code folders are replaced whole so no stale files linger. config.yaml, .env, data\ and .venv\
        # are not in the download, so they're never touched.
        foreach ($folder in Get-ChildItem -Path $src.FullName -Directory -Force) {
            $target = Join-Path $Dir $folder.Name
            if (Test-Path $target) { Remove-Item $target -Recurse -Force }
        }
        & robocopy.exe $src.FullName $Dir /E /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "copying the code into $Dir failed (robocopy code $LASTEXITCODE)" }
        $global:LASTEXITCODE = 0
    } finally {
        Remove-Item $zip -Force -ErrorAction SilentlyContinue
        Remove-Item $unpacked -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function New-VesperShortcut([string]$Path, [string]$Target, [string]$Icon, [string]$Description) {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($Path)
    $link.TargetPath = $Target
    $link.WorkingDirectory = $Dir
    $link.Description = $Description
    if ($Icon -and (Test-Path $Icon)) { $link.IconLocation = "$Icon,0" }
    $link.Save()
}

function Install-Vesper {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'   # Windows PowerShell downloads crawl with the progress bar on
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    Write-Host ''
    Write-Host 'Installing Vesper' -ForegroundColor Cyan
    Write-Host "Folder: $Dir"

    Write-Step '1/6  Checking this PC'
    if ([Environment]::OSVersion.Version.Major -lt 10) { throw 'Vesper needs Windows 10 or 11' }
    $arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    if ($arch -eq 'ARM64') {
        Write-Warn 'This is an ARM PC: Vesper runs through Windows x64 emulation, so speech is slower.'
    }
    if ((Test-Path $Dir) -and (Get-ChildItem -Path $Dir -Force | Select-Object -First 1) -and -not (Test-VesperFolder $Dir)) {
        throw "$Dir already holds other files; pick an empty folder with -Dir"
    }
    if (Test-MicrophoneAllowed) {
        Write-Note 'Desktop apps may use the microphone.'
    } else {
        Write-Warn 'Windows is blocking desktop apps from the microphone, so Vesper would hear nothing.'
        if (-not $Unattended) {
            Start-Process 'ms-settings:privacy-microphone'
            Write-Warn 'Settings is open: turn on "Microphone access" and "Let desktop apps access your microphone".'
            Read-Host '   Press Enter when done' | Out-Null
            if (-not (Test-MicrophoneAllowed)) { Write-Warn 'Still blocked. Vesper installs anyway; fix it before you talk to it.' }
        }
    }

    Write-Step '2/6  Python'
    $python = $null
    if (-not $InstallPython) { $python = Find-Python }
    if ($python) {
        Write-Note "Using Python $($python.Version): $($python.Path)"
    } else {
        Install-PythonFromPythonOrg
        $python = Find-Python
        if (-not $python) { throw 'Python was installed but this window cannot find it; close PowerShell, open a new one and paste the line again' }
        Write-Note "Installed Python $($python.Version): $($python.Path)"
    }

    $venvPython = Join-Path $Dir '.venv\Scripts\python.exe'
    $fresh = -not (Test-Path (Join-Path $Dir 'config.yaml'))
    Write-Step '3/6  Vesper'
    Stop-RunningVesper $venvPython
    Get-VesperCode

    Write-Step '4/6  Packages: speech, voices, wake words (5-10 minutes the first time)'
    $env:VESPER_PYTHON = $python.Path
    $env:VESPER_NOPAUSE = '1'
    try {
        & (Join-Path $Dir 'setup.bat')
        if ($LASTEXITCODE -ne 0) { throw 'setup.bat failed; the lines above say why' }
    } finally {
        Remove-Item Env:\VESPER_PYTHON, Env:\VESPER_NOPAUSE -ErrorAction SilentlyContinue
    }

    $icon = Join-Path $Dir 'data\vesper.ico'
    Push-Location $Dir
    try {
        $questions = @('-m', 'assistant.firstrun', '--icon', $icon)
        if ($fresh -or $Reconfigure) {
            Write-Step '5/6  A few questions'
            if ($Unattended) { $questions += '--unattended' }
            if ($Name) { $questions += @('--name', $Name) }
            if ($Goal) { $questions += @('--goal', $Goal) }
            if ($BrainUrl) { $questions += @('--brain-url', $BrainUrl) }
        } else {
            Write-Step '5/6  Settings kept (add -Reconfigure to answer the questions again)'
            $questions += '--no-questions'
        }
        & $venvPython @questions
        if ($LASTEXITCODE -ne 0) { throw 'the questions step failed; the lines above say why' }
    } finally { Pop-Location }

    Write-Step '6/6  Shortcuts'
    $desktop = [Environment]::GetFolderPath('Desktop')
    $programs = [Environment]::GetFolderPath('Programs')
    if ($desktop) { New-VesperShortcut (Join-Path $desktop 'Vesper.lnk') (Join-Path $Dir 'start.bat') $icon 'Start Vesper' }
    New-VesperShortcut (Join-Path $programs 'Vesper.lnk') (Join-Path $Dir 'start.bat') $icon 'Start Vesper'
    New-VesperShortcut (Join-Path $programs 'Update Vesper.lnk') (Join-Path $Dir 'update.bat') $icon 'Update Vesper to the newest version'
    Write-Note 'Vesper is on the desktop and in the Start menu; "Update Vesper" is in the Start menu.'

    Write-Host ''
    Write-Host "Vesper is installed in $Dir." -ForegroundColor Green
    if ($NoStart) {
        Write-Note 'Start it with the Vesper icon on the desktop.'
    } else {
        Start-Process -FilePath (Join-Path $Dir 'start.bat') -WorkingDirectory $Dir
        Write-Note 'Starting: a black log window (keep it open), then the HUD. The first start downloads'
        Write-Note 'the speech models and voice, about 200 MB.'
    }
    Write-Note 'Next: HUD > Setup > Your voice > Calibrate my voice, then say "Vesper, what is connected?"'
}

try {
    Install-Vesper
    $global:LASTEXITCODE = 0
} catch {
    Write-Host ''
    Write-Host "Install stopped: $($_.Exception.Message)." -ForegroundColor Red
    Write-Host 'Your settings and files are untouched. Fix that, then paste the same line again.'
    $global:LASTEXITCODE = 1
    if ($PSCommandPath) { exit 1 }   # run as a file (CI): fail the run. Pasted: keep the window open.
}
