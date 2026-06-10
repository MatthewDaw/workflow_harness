/**
 * Bundle-membership helpers shared by every catalog surface that collapses
 * bundle members under their bundle (skills and agents alike).
 */

/**
 * Names that are members of any resolved bundle (transitive leaves preferred).
 * A plain item whose name is in this set is only reachable via its bundle.
 */
export function bundleMemberNames(
  items: readonly { kind: string; members: string[]; resolvedMembers?: string[] }[],
): Set<string> {
  const names = new Set<string>();
  for (const it of items) {
    if (it.kind !== 'bundle') continue;
    for (const m of it.resolvedMembers ?? it.members) names.add(m);
  }
  return names;
}
