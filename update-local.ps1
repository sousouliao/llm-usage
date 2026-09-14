<#
.SYNOPSIS
Sync, collect local usage on Windows, commit raw data, and push it.

.EXAMPLE
.\update-local.ps1
.\update-local.ps1 -Since 2026-07-01
#>
[CmdletBinding()]
param(
    [string]$Since = $env:SINCE
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
Set-Location -LiteralPath $PSScriptRoot

$env:GIT_TERMINAL_PROMPT = "0"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

# Local secrets (DeepSeek userToken, etc.). The scheduled task does not inherit
# an interactive shell, so they live in .env rather than the user profile.
$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path -LiteralPath $envFile) {
    Get-Content -LiteralPath $envFile | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $eq = $line.IndexOf("=")
        if ($eq -lt 1) { return }
        $name = $line.Substring(0, $eq).Trim()
        $value = $line.Substring($eq + 1).Trim()
        if (($value.StartsWith("'") -and $value.EndsWith("'")) -or
            ($value.StartsWith('"') -and $value.EndsWith('"'))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        Set-Item -Path "Env:$name" -Value $value
    }
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    foreach ($gitDir in @(
        "C:\Program Files\Git\cmd",
        "C:\Program Files\Git\bin",
        "${env:ProgramFiles(x86)}\Git\cmd",
        (Join-Path $env:LOCALAPPDATA "Programs\Git\cmd")
    )) {
        if ($gitDir -and (Test-Path -LiteralPath (Join-Path $gitDir "git.exe"))) {
            $env:PATH = "$gitDir;$env:PATH"
            break
        }
    }
}

# Two kinds of files this machine writes but must never carry into a merge:
#
# Artifacts (stats.json / SVG) are owned by GitHub Actions. A leftover manual
# run.py without --collect-only can still dirty them. Raw is a full re-collect,
# and both machines write the same account-level file. Syncing with either kind
# dirty collides with the remote, and a leftover conflict wedges every later
# run. Both are regenerable, so discarding them is safe.
$ciOwnedPaths = @("data/stats.json", "assets")
$generatedPaths = $ciOwnedPaths + @("data/raw")
$commitMessage = "chore(data): usage raw @ $(Get-Date -Format 'yyyy-MM-dd')"
$transcriptStarted = $false

function Restore-Generated {
    git checkout -f HEAD -- @generatedPaths 2>$null | Out-Null
}

function Restore-CiOwned {
    git checkout -f HEAD -- @ciOwnedPaths 2>$null | Out-Null
}

function Get-UnmergedFiles {
    $raw = git diff --name-only --diff-filter=U
    if ($LASTEXITCODE -ne 0) { throw "git diff failed." }
    if ([string]::IsNullOrWhiteSpace($raw)) { return @() }
    @($raw -split "`n" | Where-Object { $_.Trim().Length -gt 0 })
}

function Invoke-Collect {
    $runArgs = @("run.py", "--collect-only")
    if ($Since) { $runArgs += @("--since", $Since) }
    & $script:pythonCommand @script:pythonPrefix @runArgs
    if ($LASTEXITCODE -ne 0) { throw "Collection failed with exit code $LASTEXITCODE." }
    # --collect-only does not write artifacts; this only clears a leftover full run.
    Restore-CiOwned
}

function Invoke-CommitRaw {  # $true when a commit was created
    git add data/raw
    if ($LASTEXITCODE -ne 0) { throw "git add failed." }
    git diff --cached --quiet
    if ($LASTEXITCODE -eq 0) { return $false }
    if ($LASTEXITCODE -ne 1) { throw "git diff failed." }
    git commit -m $commitMessage
    if ($LASTEXITCODE -ne 0) { throw "git commit failed." }
    return $true
}

function Start-UpdateLog {
    $logDir = Join-Path $env:LOCALAPPDATA "llm-usage"
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $logFile = Join-Path $logDir "update.log"
    if ((Test-Path -LiteralPath $logFile) -and
        ((Get-Item -LiteralPath $logFile).Length -gt 1MB)) {
        Remove-Item -LiteralPath $logFile -Force
    }
    Start-Transcript -Path $logFile -Append | Out-Null
}

function Invoke-Update {
    $venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $script:pythonCommand = $venvPython
        $script:pythonPrefix = @()
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        $script:pythonCommand = "py"
        $script:pythonPrefix = @("-3")
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $script:pythonCommand = "python"
        $script:pythonPrefix = @()
    } else {
        throw "Python 3 was not found. Create .venv or install Python 3.12+."
    }

    # Self-heal whatever a previous conflicted run left behind.
    git rebase --abort 2>$null | Out-Null
    Restore-Generated
    while ($true) {
        $top = git stash list -n 1
        if (-not $top -or $top -notmatch '^stash@\{0\}: autostash$') { break }
        Write-Host "==> Drop leftover autostash"
        git stash drop | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "git stash drop failed." }
    }
    $unmerged = Get-UnmergedFiles
    if ($unmerged.Count -gt 0) {
        throw "Unresolved conflicts need manual handling:`n$($unmerged -join "`n")"
    }

    # Sync before collecting. Raw and artifacts match HEAD at this point, so the
    # autostash cannot contain them and cannot collide with the remote.
    Write-Host "==> Sync remote"
    git pull --rebase --autostash origin main
    if ($LASTEXITCODE -ne 0) { throw "git pull failed." }

    Write-Host "==> Collect raw data"
    Invoke-Collect

    Write-Host "==> Commit raw data"
    if (-not (Invoke-CommitRaw)) {
        Write-Host "No changes; skipping commit."
        return
    }

    git push origin main
    if ($LASTEXITCODE -eq 0) {
        Write-Host "==> Pushed. GitHub Actions will rebuild stats.json and SVG assets."
        return
    }

    # The remote moved while we were collecting (CI artifacts, or the other
    # machine's raw). No merge needed: raw is a full re-collect, so redoing it
    # on top of the newest remote yields the complete current state.
    Write-Host "==> Push was rejected; re-collect on top of the newest remote"
    git fetch origin main
    if ($LASTEXITCODE -ne 0) { throw "git fetch failed." }

    $ahead = (git rev-list --count FETCH_HEAD..HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw "git rev-list failed." }
    $dirty = git status --porcelain -- ':(exclude)data/raw'
    if ($LASTEXITCODE -ne 0) { throw "git status failed." }
    if ($ahead -ne "1" -or -not [string]::IsNullOrWhiteSpace($dirty)) {
        throw "Other unpushed commits or local edits exist; not retrying automatically."
    }

    git reset --hard FETCH_HEAD
    if ($LASTEXITCODE -ne 0) { throw "git reset --hard failed." }
    Invoke-Collect
    if (-not (Invoke-CommitRaw)) {
        Write-Host "No changes; skipping commit."
        return
    }
    git push origin main
    if ($LASTEXITCODE -ne 0) { throw "git push failed." }
    Write-Host "==> Pushed. GitHub Actions will rebuild stats.json and SVG assets."
}

try {
    try {
        Start-UpdateLog
        $transcriptStarted = $true
    } catch {
        Write-Host "[warn] Could not start transcript: $_"
    }
    Invoke-Update
} finally {
    if ($transcriptStarted) {
        Stop-Transcript | Out-Null
    }
}
