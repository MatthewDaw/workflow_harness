# ralph.ps1 — driver for the agent-families Ralph loop. Run from the worktree root.
# Persistence lives HERE, not in the prompt: this loop re-invokes a fresh agent per
# iteration, externally verifies its work, and only accepts completion structurally.

$promptPath  = "agent-families\RALPH_PROMPT.md"
$progress    = "agent-families\PROGRESS.md"
New-Item -ItemType Directory -Force "ralph-logs" | Out-Null
$prompt = Get-Content -Raw $promptPath
$iter = 0

while ($iter -lt 300) {   # hard safety cap
  $iter++
  $head = (Get-Content $progress -TotalCount 1)

  # --- structural completion check (never trust the agent's own declaration) ---
  if ($head -match "COMPLETE") {
    $unchecked = (Select-String -Path $progress -Pattern '^\- \[ \]' -AllMatches).Count
    $auditOk   = Test-Path "agent-families\AUDIT.md"
    if ($unchecked -eq 0 -and $auditOk) { Write-Host "VERIFIED COMPLETE after $iter iterations."; break }
    Write-Host "Agent declared COMPLETE but $unchecked units unchecked / audit missing: $auditOk — rejecting."
    (Get-Content $progress) -replace '^# Status: COMPLETE', '# Status: active' | Set-Content $progress
  }
  if ($head -match "blocked") { Write-Host "Loop halted: human decision needed (see Blockers in PROGRESS.md)."; break }

  Write-Host "=== Iteration $iter  $(Get-Date -Format o) ==="
  $prompt | claude -p --dangerously-skip-permissions --output-format json `
    | Out-File -Encoding utf8 "ralph-logs\iter-$iter.json"

  # --- external verification gate: reject red-suite iterations outright ---
  if (Test-Path "agent-families\pyproject.toml") {
    Push-Location agent-families
    uv run pytest -q; $green = ($LASTEXITCODE -eq 0)
    Pop-Location
    if (-not $green) {
      Write-Host "Suite RED after iteration $iter — reverting its commit."
      git reset --hard HEAD~1
      Add-Content $progress "`n- DRIVER: iteration $iter reverted (red suite)"
    }
  }
  Start-Sleep -Seconds 30   # backoff; quota-window exhaustion just retries later
}
