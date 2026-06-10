# ralph-worker.ps1 - builds ONE unit to green inside a given dir (worktree or main repo).
# Standalone (spawned as its own process for parallelism). Writes a .result file:
# the green commit sha, or "FAILED". Pure ASCII.
param(
  [Parameter(Mandatory=$true)][string]$Unit,   # e.g. 001/U4
  [Parameter(Mandatory=$true)][string]$Dir,    # build dir (worktree or repo root)
  [Parameter(Mandatory=$true)][string]$Repo    # repo root (for shared prompt/logs/model)
)
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue

$plan,$uid = $Unit -split '/'
$basePrompt = Get-Content -Raw (Join-Path $Repo "agent-families\RALPH_PROMPT.md")
$modelFile  = Join-Path $Repo "agent-families\MODEL.txt"
$resultF    = Join-Path $Repo "ralph-logs\$plan-$uid.result"
Remove-Item $resultF -EA SilentlyContinue

$scoped = @"
$basePrompt

== WAVE-SCOPED OVERRIDE (this invocation) ==
Build EXACTLY ONE unit: $plan / $uid. Find it in docs/plans/2026-06-10-$plan-*.md.
- Implement ONLY that unit's Files, Approach, and every Test scenario.
- Do NOT touch other units' files. Do NOT edit pyproject.toml, uv.lock,
  agent-families/tests/conftest.py, agent-families/PROGRESS.md, DAG.md, MODEL.txt,
  RALPH_PROMPT.md, or any ralph*.ps1. If you truly need a NEW pip dependency, do not
  edit pyproject - instead append its name to agent-families/NEED_DEP.txt and continue
  using what is available.
- Verify with:  uv run --frozen pytest -q   (must be fully green).
- Commit ONLY your unit's source + test files, message:
  'feat(agent-families): plan-$plan $uid - <unit name>'. Then exit. Do not flip PROGRESS.
"@

for ($k=1; $k -le 6; $k++) {
  $model = (Get-Content $modelFile -Raw -EA SilentlyContinue); if ($model) { $model = $model.Trim() } else { $model = "fable" }
  $before = (git -C $Dir rev-parse HEAD).Trim()
  $log = Join-Path $Repo "ralph-logs\$plan-$uid-try$k.json"
  Write-Host "[$Unit] attempt $k model=$model dir=$Dir"
  Push-Location $Dir
  $scoped | claude -p --model $model --dangerously-skip-permissions --output-format json | Out-File -Encoding utf8 $log
  $ec = $LASTEXITCODE
  Pop-Location
  $quota = $false
  try { $o = Get-Content $log -Raw | ConvertFrom-Json; if ($o.is_error -and ("$($o.subtype) $($o.result) $($o.error)" -match "(?i)limit|quota|rate|exhaust")) { $quota = $true } } catch {}
  $after = (git -C $Dir rev-parse HEAD).Trim()
  if ($ec -ne 0 -and $after -eq $before) { $quota = $true }
  if ($quota) { Write-Host "[$Unit] quota wall - sleep 30m"; Start-Sleep -Seconds 1800; continue }
  if ($after -ne $before) {
    Push-Location (Join-Path $Dir "agent-families")
    uv run --frozen pytest -q *> (Join-Path $Repo "ralph-logs\$plan-$uid-pytest$k.txt")
    $green = ($LASTEXITCODE -eq 0)
    Pop-Location
    if ($green) { Write-Host "[$Unit] GREEN $after"; Set-Content $resultF $after -Encoding ascii; exit 0 }
    Write-Host "[$Unit] red - revert + retry"; git -C $Dir reset --hard $before | Out-Null
  } else { Write-Host "[$Unit] no commit - retry" }
}
Set-Content $resultF "FAILED" -Encoding ascii
Write-Host "[$Unit] FAILED"; exit 1
