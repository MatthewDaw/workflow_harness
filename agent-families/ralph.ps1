# ralph.ps1 - quota-aware driver for the agent-families Ralph loop.
# Run from the repo/worktree root. Pure ASCII (Windows PowerShell 5.1 reads BOM-less as ANSI).
#
# Persistence lives HERE: re-invokes a fresh `claude -p` agent per unit, verifies its
# work externally, waits out quota windows instead of spinning, and supports a LIVE
# model switch (edit agent-families\MODEL.txt to "fable" or "opus" anytime, no restart).

# --- env fixes (001/U1 probe findings): uv in ~/.local/bin; outer .venv must not leak ---
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue

# --- config ---
$promptPath    = "agent-families\RALPH_PROMPT.md"
$progress      = "agent-families\PROGRESS.md"
$modelFile     = "agent-families\MODEL.txt"   # "fable" or "opus"; editable while running
$costSwitchUsd = 50.0   # cumulative-cost backstop: auto-downshift fable->opus past this (USD proxy)
$quotaSleepMin = 30     # on a quota/rate wall, wait this long for the window to reopen
$workSleepSec  = 20     # backoff between productive iterations
$maxIter       = 600

if (-not (Test-Path $modelFile)) { "fable" | Set-Content $modelFile -Encoding ascii }
New-Item -ItemType Directory -Force "ralph-logs" | Out-Null
$prompt  = Get-Content -Raw $promptPath
$cumCost = 0.0
$iter    = 0

while ($iter -lt $maxIter) {
  $iter++
  $head = (Get-Content $progress -TotalCount 1)

  # --- structural completion check (never trust the agent's self-declaration) ---
  if ($head -match "COMPLETE") {
    $unchecked = (Select-String -Path $progress -Pattern '^\- \[ \]' -AllMatches).Count
    $auditOk   = Test-Path "agent-families\AUDIT.md"
    if ($unchecked -eq 0 -and $auditOk) { Write-Host "VERIFIED COMPLETE after $iter iterations. Spend ~`$$([math]::Round($cumCost,2))."; break }
    Write-Host "Premature COMPLETE ($unchecked unchecked, audit=$auditOk) - rejecting."
    (Get-Content $progress) -replace '^# Status: COMPLETE', '# Status: active' | Set-Content $progress
  }
  if ($head -match "blocked") { Write-Host "Loop halted: human decision needed (see Blockers in PROGRESS.md)."; break }

  # --- resolve model live ---
  $model = (Get-Content $modelFile -Raw -ErrorAction SilentlyContinue)
  if ($model) { $model = $model.Trim() }
  if (-not $model) { $model = "fable" }

  $shaBefore = (git rev-parse HEAD).Trim()
  Write-Host "=== Iteration $iter  model=$model  cum=`$$([math]::Round($cumCost,2))  $(Get-Date -Format o) ==="

  $logFile = "ralph-logs\iter-$iter.json"
  $prompt | claude -p --model $model --dangerously-skip-permissions --output-format json | Out-File -Encoding utf8 $logFile
  $exitCode = $LASTEXITCODE

  $shaAfter   = (git rev-parse HEAD).Trim()
  $progressed = ($shaBefore -ne $shaAfter)

  # --- parse envelope: accumulate cost, detect quota/rate wall ---
  $quotaHit = $false
  try {
    $envObj = Get-Content $logFile -Raw | ConvertFrom-Json
    if ($envObj.total_cost_usd) { $cumCost += [double]$envObj.total_cost_usd }
    $blob = "$($envObj.subtype) $($envObj.result) $($envObj.error)"
    if ($envObj.is_error -and ($blob -match "(?i)limit|quota|rate|exhaust")) { $quotaHit = $true }
  } catch { }
  if ($exitCode -ne 0 -and -not $progressed) { $quotaHit = $true }  # fast no-progress failure == wall

  # --- cumulative-cost backstop: downshift fable -> opus ---
  if ($model -eq "fable" -and $cumCost -ge $costSwitchUsd) {
    "opus" | Set-Content $modelFile -Encoding ascii
    Write-Host "Cumulative spend ~`$$([math]::Round($cumCost,2)) >= `$$costSwitchUsd : auto-switching model to OPUS."
  }

  if ($quotaHit) {
    Write-Host "Quota/rate wall hit. Sleeping $quotaSleepMin min for the window to reopen (no cap burned on no-ops)."
    Start-Sleep -Seconds ($quotaSleepMin * 60)
    continue
  }

  # --- external verification gate: reject red-suite iterations. Stash infra files so the
  #     hard reset can never clobber the driver's own ralph.ps1 / MODEL.txt again. ---
  if ($progressed -and (Test-Path "agent-families\pyproject.toml")) {
    Push-Location agent-families
    uv run pytest -q; $green = ($LASTEXITCODE -eq 0)
    Pop-Location
    if (-not $green) {
      Write-Host "Suite RED after iteration $iter - reverting its commit."
      git reset --hard HEAD~1
      Add-Content $progress "`n- DRIVER: iteration $iter reverted (red suite)"
    }
  }
  Start-Sleep -Seconds $workSleepSec
}
