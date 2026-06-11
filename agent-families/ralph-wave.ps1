# ralph-wave.ps1 - DAG-parallel orchestrator. Runs the wave schedule (DAG.md):
# solo waves build in the main tree; 2-wide waves spawn parallel worktree workers,
# then cherry-pick the disjoint commits, gate on the full suite, and FALL BACK to
# serial on a red merge so parallel speculation can never corrupt integration.
# Pure ASCII. Run from repo root.

$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
$repo     = (Resolve-Path ".").Path
$progress = "agent-families\PROGRESS.md"
$worker   = Join-Path $repo "agent-families\ralph-worker.ps1"
New-Item -ItemType Directory -Force "ralph-logs" | Out-Null

$waves = @(
  ,@("001/U4")
  ,@("001/U5","001/U7")
  ,@("001/U6","001/U8")
  ,@("001/U9")
  ,@("002/U1","002/U2")
  ,@("002/U3")
  ,@("002/U4","002/U5")
  ,@("002/U6","002/U7")
  ,@("002/U8")
  ,@("003/U1","003/U2")
  ,@("003/U3","003/U4")
  ,@("003/U5","003/U7")
  ,@("003/U6","003/U8")
  ,@("003/U9")
  ,@("004/U1","004/U4")
  ,@("004/U2","004/U5")
  ,@("004/U3","004/U6")
  ,@("004/U7")
  ,@("004/U8")
  ,@("004/U9")
  ,@("005/U1","005/U2","005/U3","005/U4","005/U5","005/U6")
  ,@("005/U7")
  # --- Plan 007: Greenfield Mode (queued after 001-005). Waves follow the plan's DAG. ---
  ,@("007/U1")
  ,@("007/U2","007/U4","007/U10")
  ,@("007/U3","007/U5","007/U13a")
  ,@("007/U5b")
  ,@("007/U6")
  ,@("007/U7")
  ,@("007/U8")
  ,@("007/U9","007/U13b")
  ,@("007/U11","007/U12")
)

function Unit-Done($u) { $p,$x = $u -split '/'; return ((Select-String -Path $progress -Pattern "^\- \[x\] $p/$x " -EA SilentlyContinue).Count -gt 0) }
function Flip($u) { $p,$x = $u -split '/'; (Get-Content $progress) -replace "^\- \[ \] $p/$x ","- [x] $p/$x " | Set-Content $progress; git add $progress | Out-Null; git commit -q -m "chore(agent-families): mark $u done" | Out-Null }
function Run-Worker($u,$dir) { $p,$x = $u -split '/'; & $worker -Unit $u -Dir $dir -Repo $repo *> (Join-Path $repo "ralph-logs\$p-$x.worker.txt"); return (Get-Content (Join-Path $repo "ralph-logs\$p-$x.result") -EA SilentlyContinue) }
function Suite-Green { Push-Location (Join-Path $repo "agent-families"); uv run --frozen pytest -q *> (Join-Path $repo "ralph-logs\gate.txt"); $g = ($LASTEXITCODE -eq 0); if (-not $g) { Write-Host "  gate red - retrying once (flaky-test absorb)"; uv run --frozen pytest -q *> (Join-Path $repo "ralph-logs\gate-retry.txt"); $g = ($LASTEXITCODE -eq 0) }; Pop-Location; return $g }

foreach ($wave in $waves) {
  $todo = @($wave | Where-Object { -not (Unit-Done $_) })
  if ($todo.Count -eq 0) { continue }
  if ((Get-Content $progress -TotalCount 1) -match "blocked") { Write-Host "BLOCKED - stopping."; break }

  # REVIEW CHECKPOINT: pause before the novel phases (Plan 4 retrieval/reflector, Plan 5 router/boundary)
  # so a human reviews the prior plan + the required-acceptance-test conformance before barreling on.
  $wavePlan = ($todo[0] -split '/')[0]
  if (($wavePlan -eq "004" -or $wavePlan -eq "005") -and -not (Test-Path "agent-families\REVIEW-OK-$wavePlan.txt")) {
    Write-Host "=== REVIEW CHECKPOINT before Plan $wavePlan - pausing for human review. ==="
    Add-Content $progress "`n- REVIEW CHECKPOINT: paused before Plan $wavePlan (novel phase). To proceed: review the prior plan's units + their ## Conformance notes, then create agent-families\REVIEW-OK-$wavePlan.txt and relaunch ralph-wave.ps1."
    break
  }

  # HUMAN STEP: 007/U5b is a manual human-as-founder trial (no production code) - the loop cannot do it.
  if (($todo -contains "007/U5b") -and -not (Test-Path "agent-families\U5B-DONE.txt")) {
    Write-Host "=== HUMAN STEP 007/U5b - pausing for the manual human-as-founder trial. ==="
    Add-Content $progress "`n- HUMAN STEP: 007/U5b is a manual human-as-founder trial (no production code). Run it per the plan, record transcripts under docs/, flip 007/U5b to [x] in this file, create agent-families\U5B-DONE.txt, then relaunch ralph-wave.ps1."
    break
  }

  if ($todo.Count -eq 1) {
    Write-Host "=== SOLO: $($todo[0])  $(Get-Date -Format o) ==="
    $r = Run-Worker $todo[0] $repo
    if ($r -and $r -ne "FAILED") { if (Suite-Green) { Flip $todo[0]; Write-Host "  solo $($todo[0]) DONE" } else { Write-Host "  solo gate red"; "`n# Status: blocked" | Add-Content $progress; "`n- BLOCKED: $($todo[0]) gate red after solo build" | Add-Content $progress; break } }
    else { "`n# Status: blocked" | Add-Content $progress; "`n- BLOCKED: $($todo[0]) solo build failed" | Add-Content $progress; break }
    continue
  }

  # 2-wide parallel
  Write-Host "=== PARALLEL: $($todo -join ' || ')  $(Get-Date -Format o) ==="
  $base = (git rev-parse HEAD).Trim()
  $procs = @()
  foreach ($u in $todo) {
    $p,$x = $u -split '/'
    $wt = Join-Path (Split-Path $repo -Parent) "af-wt-$p-$x"
    git worktree remove --force $wt 2>$null | Out-Null
    Remove-Item -Recurse -Force $wt -EA SilentlyContinue
    git worktree add --detach $wt $base | Out-Null
    $procs += @{ unit=$u; wt=$wt; proc=(Start-Process powershell.exe -PassThru -WindowStyle Hidden -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File",$worker,"-Unit",$u,"-Dir",$wt,"-Repo",$repo) }
  }
  $procs | ForEach-Object { $_.proc.WaitForExit() }

  # PARTIAL MERGE: cherry-pick every unit that built green (disjoint -> clean); only the
  # rest (build-failed or cherry-pick-conflict) serialize. One bad unit no longer nukes the wave.
  $serialize = @()
  $picked = @()
  $preMerge = (git rev-parse HEAD).Trim()   # HEAD right before cherry-picks; revert to THIS, never wave-start $base, so newer commits survive
  foreach ($pc in $procs) {
    $r = Get-Content (Join-Path $repo ("ralph-logs\" + ($pc.unit -replace '/','-') + ".result")) -EA SilentlyContinue
    if ($r -and $r -ne "FAILED") {
      git cherry-pick $r 2>&1 | Out-Null
      if ($LASTEXITCODE -eq 0) { $picked += $pc.unit; Write-Host "  picked $($pc.unit)" }
      else { git cherry-pick --abort 2>$null | Out-Null; $serialize += $pc.unit; Write-Host "  cherry-pick conflict $($pc.unit) -> serial" }
    } else { $serialize += $pc.unit; Write-Host "  build failed $($pc.unit) -> serial" }
  }
  # gate the merged successes together; if the union is red, revert them and serialize all
  if ($picked.Count -gt 0) {
    if (Suite-Green) { foreach ($u in $picked) { Flip $u }; Write-Host "  merged $($picked.Count) green: $($picked -join ', ')" }
    else { Write-Host "  merged suite red (twice) - reverting picked, serializing"; git reset --hard $preMerge | Out-Null; $serialize = @($picked) + $serialize }
  }
  # cleanup worktrees
  foreach ($pc in $procs) { git worktree remove --force $pc.wt 2>$null | Out-Null; Remove-Item -Recurse -Force $pc.wt -EA SilentlyContinue }
  # serial fallback only for the units that did not cleanly merge
  $bail = $false
  foreach ($u in $serialize) {
    if (Unit-Done $u) { continue }
    Write-Host "  SERIAL: $u"
    $r = Run-Worker $u $repo
    if ($r -and $r -ne "FAILED" -and (Suite-Green)) { Flip $u } else { Write-Host "  serial $u failed"; $c = Get-Content $progress; $c[0] = "# Status: blocked"; $c | Set-Content $progress; "`n- BLOCKED: $u failed in serial fallback" | Add-Content $progress; $bail = $true; break }
  }
  if ($bail) { break }
}
Write-Host "=== wave driver exit. done: $((Select-String -Path $progress -Pattern '^\- \[x\]').Count)/42  $(Get-Date -Format o) ==="
