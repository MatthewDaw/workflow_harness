/**
 * Bundle expansion shared by every catalog item kind that supports bundles
 * (skills and agents). A bundle's `members` are item names; a member may itself
 * be a bundle (nesting), and resolution is transitive.
 */

/**
 * Flatten a bundle's members transitively into the set of leaf-item names.
 * Nested bundles are expanded; a cycle is guarded by a visited set. A member
 * absent from `byName` is kept as a leaf (a dangling ref is the caller's call).
 */
export function flattenBundle<T extends { kind: string; members: string[] }>(
  bundle: { members: string[] },
  byName: Map<string, T>,
  seen = new Set<string>(),
): string[] {
  const leaves: string[] = [];
  for (const memberName of bundle.members) {
    if (seen.has(memberName)) continue;
    seen.add(memberName);
    const member = byName.get(memberName);
    if (member?.kind === 'bundle') {
      leaves.push(...flattenBundle(member, byName, seen));
    } else {
      leaves.push(memberName);
    }
  }
  return [...new Set(leaves)];
}
