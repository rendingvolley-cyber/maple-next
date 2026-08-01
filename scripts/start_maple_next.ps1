#Requires -Version 5.1
<#
.SYNOPSIS
    Official Windows launcher for Maple Next (Issue #31 Lane A).

.DESCRIPTION
    Resolves the repository root from this script's own location (never the
    caller's current working directory), resolves a Python 3.11 interpreter,
    resolves the official runtime root (repository-external), and starts the
    app via the existing `python -m maple_next --database ... --export-directory ...`
    entrypoint. Does not read, write, or print `.env`. Does not create or
    delete runtime state beyond `mkdir -p`-style directory creation.

.PARAMETER RuntimeRoot
    Overrides the runtime root (highest priority). Falls back to
    MAPLE_NEXT_RUNTIME_ROOT, then %LOCALAPPDATA%\MapleNext\Battle1, then
    %USERPROFILE%\.maple-next\Battle1. Must resolve outside the repository
    checkout (never the repository root itself or any descendant of it) --
    this is verified, fail-closed, before any directory or file is created.

.PARAMETER Smoke
    Runs the repository-external field-use smoke suite instead of the app.

.EXAMPLE
    scripts\start_maple_next.cmd
.EXAMPLE
    scripts\start_maple_next.cmd --smoke
.EXAMPLE
    scripts\start_maple_next.cmd --runtime-root "D:\Maple Runtime"
.EXAMPLE
    scripts\start_maple_next.cmd --smoke --runtime-root "D:\Maple Runtime"
#>
[CmdletBinding()]
param(
    [string]$RuntimeRoot,
    [switch]$Smoke
)

$ErrorActionPreference = "Stop"

# Repository root is resolved from this script's own path, two levels up
# from scripts\start_maple_next.ps1 -- never from the caller's CWD.
$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$ScriptsDirectory = $PSScriptRoot

function Resolve-MaplePython {
    param([string]$RepoRoot)

    $venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        return $venvPython
    }

    $pyLauncher = Get-Command "py" -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        try {
            & $pyLauncher.Source "-3.11" "-c" "import sys" *> $null
            if ($LASTEXITCODE -eq 0) {
                return @($pyLauncher.Source, "-3.11")
            }
        } catch {
            # fall through to fail-closed below
        }
    }

    return $null
}

$PythonCommand = Resolve-MaplePython -RepoRoot $RepositoryRoot
if (-not $PythonCommand) {
    Write-Error @"
Maple Next launcher: could not resolve a Python 3.11 interpreter.
Checked, in order:
  1. $RepositoryRoot\.venv\Scripts\python.exe
  2. `py -3.11` (Python launcher for Windows)
Install Python 3.11 and either create $RepositoryRoot\.venv or ensure
the `py` launcher can resolve -3.11, then retry.
"@
    exit 1
}

if ($PythonCommand -is [array]) {
    $PythonExe = $PythonCommand[0]
    $PythonArgsPrefix = $PythonCommand[1..($PythonCommand.Length - 1)]
} else {
    $PythonExe = $PythonCommand
    $PythonArgsPrefix = @()
}

# Runtime root resolution priority: -RuntimeRoot > MAPLE_NEXT_RUNTIME_ROOT >
# %LOCALAPPDATA%\MapleNext\Battle1 > %USERPROFILE%\.maple-next\Battle1.
if ($RuntimeRoot) {
    $ResolvedRuntimeRoot = $RuntimeRoot
} elseif ($env:MAPLE_NEXT_RUNTIME_ROOT) {
    $ResolvedRuntimeRoot = $env:MAPLE_NEXT_RUNTIME_ROOT
} elseif ($env:LOCALAPPDATA) {
    $ResolvedRuntimeRoot = Join-Path $env:LOCALAPPDATA "MapleNext\Battle1"
} else {
    $ResolvedRuntimeRoot = Join-Path $env:USERPROFILE ".maple-next\Battle1"
}

function ConvertTo-CanonicalWindowsPath {
    <#
    Resolves a path to an absolute, `.`/`..`-collapsed, separator-normalized
    form. Windows PowerShell 5.1 runs on .NET Framework, which has no
    [System.IO.Path]::TrimEndingDirectorySeparator, so the trailing-separator
    normalization it would provide is done manually here instead (while
    still keeping a bare drive root such as "C:\" intact).
    #>
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = [System.IO.Path]::GetFullPath($Path)
    $trimmed = $full.TrimEnd('\', '/')
    if ($trimmed.Length -eq 2 -and $trimmed[1] -eq ':') {
        $trimmed = $trimmed + '\'
    }
    return $trimmed
}

function Test-RuntimeRootInsideRepository {
    <#
    Fail-closed, case-insensitive check of whether $RuntimeRoot is the
    repository root itself or a true descendant of it, after canonicalizing
    both paths. Never a raw string-prefix compare: a sibling directory that
    merely starts with the repository directory's name (e.g.
    "maple-next-runtime" next to "maple-next") must not be rejected.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$RepositoryRoot,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot
    )

    $repositoryFull = ConvertTo-CanonicalWindowsPath -Path $RepositoryRoot
    $runtimeFull = ConvertTo-CanonicalWindowsPath -Path $RuntimeRoot

    $comparison = [System.StringComparison]::OrdinalIgnoreCase
    $repositoryPrefix = $repositoryFull.TrimEnd('\') + [System.IO.Path]::DirectorySeparatorChar

    $isSame = [string]::Equals($runtimeFull, $repositoryFull, $comparison)
    $isChild = $runtimeFull.StartsWith($repositoryPrefix, $comparison)

    return [PSCustomObject]@{
        IsInside       = ($isSame -or $isChild)
        RepositoryFull = $repositoryFull
        RuntimeFull    = $runtimeFull
    }
}

# Fail-closed runtime-root validation MUST run before any directory, log
# file, database, or export artifact is created -- covers the -RuntimeRoot
# parameter, MAPLE_NEXT_RUNTIME_ROOT, and both LOCALAPPDATA/USERPROFILE
# fallbacks, since $ResolvedRuntimeRoot above already folds all four inputs
# into one value.
$RuntimeRootCheck = Test-RuntimeRootInsideRepository -RepositoryRoot $RepositoryRoot -RuntimeRoot $ResolvedRuntimeRoot
if ($RuntimeRootCheck.IsInside) {
    Write-Error @"
Maple Next launcher: runtime root must be outside repository.
resolved repository path: $($RuntimeRootCheck.RepositoryFull)
resolved runtime path: $($RuntimeRootCheck.RuntimeFull)
"@
    exit 1
}

$StateDirectory = Join-Path $ResolvedRuntimeRoot "state"
$ExportsDirectory = Join-Path $ResolvedRuntimeRoot "exports"
$LogsDirectory = Join-Path $ResolvedRuntimeRoot "logs"
$SmokeDirectory = Join-Path $ResolvedRuntimeRoot "smoke"

foreach ($directory in @($StateDirectory, $ExportsDirectory, $LogsDirectory, $SmokeDirectory)) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}

$DatabasePath = Join-Path $StateDirectory "maple-next.db"
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogsDirectory "maple-next-$Timestamp.log"

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $LogPath -Value $line
    Write-Host $line
}

New-Item -ItemType File -Force -Path $LogPath | Out-Null
Write-Log "resolved repository root: $RepositoryRoot"
Write-Log "resolved python executable: $PythonExe $($PythonArgsPrefix -join ' ')"
Write-Log "resolved runtime root: $ResolvedRuntimeRoot"
Write-Log "database path: $DatabasePath"
Write-Log "export directory: $ExportsDirectory"
Write-Log "log path: $LogPath"

# UGREEN/device availability is not a startup precondition in this lane --
# capture/OCR integration (Lane C) determines availability at runtime and
# falls back to manual-safe continuation without failing the launcher.
Write-Log "manual-safe startup status: ENABLED (device availability is not required to start)"

if ($Smoke) {
    $SmokeScript = Join-Path $ScriptsDirectory "field_use_smoke.py"
    $SmokeSummaryPath = Join-Path $SmokeDirectory "latest.json"
    Write-Log "mode: field-use smoke"
    $smokeArgs = $PythonArgsPrefix + @(
        $SmokeScript,
        "--repository-root", $RepositoryRoot,
        "--summary-path", $SmokeSummaryPath
    )
    & $PythonExe @smokeArgs
    $ExitCode = $LASTEXITCODE
    Write-Log "smoke summary path: $SmokeSummaryPath"
} else {
    Write-Log "mode: application launch"
    $appArgs = $PythonArgsPrefix + @(
        "-m", "maple_next",
        "--database", $DatabasePath,
        "--export-directory", $ExportsDirectory
    )
    & $PythonExe @appArgs
    $ExitCode = $LASTEXITCODE
}

Write-Log "process exit code: $ExitCode"
exit $ExitCode
