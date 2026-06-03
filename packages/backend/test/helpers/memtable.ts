import {
  DeleteCommand,
  GetCommand,
  PutCommand,
  QueryCommand,
  UpdateCommand,
} from '@aws-sdk/lib-dynamodb';
import type { AwsStub } from 'aws-sdk-client-mock';

/**
 * A small in-memory DynamoDB document table for handler tests. It honours the
 * exact command shapes the Repo emits: conditional PutCommand
 * (`attribute_not_exists(PK)`), GetCommand, DeleteCommand, and QueryCommand with
 * `PK = :pk AND begins_with(SK, :sk)`. Enough to run the WS handlers end-to-end
 * through the real Repo without touching AWS.
 */

interface KeyShape {
  PK: string;
  SK: string;
}
const keyOf = (item: KeyShape): string => `${item.PK}::${item.SK}`;

class ConditionalCheckFailed extends Error {
  override name = 'ConditionalCheckFailedException';
}

export function installInMemoryTable(
  ddbMock: AwsStub<unknown, unknown, unknown>,
): Map<string, Record<string, unknown>> {
  const store = new Map<string, Record<string, unknown>>();

  ddbMock.on(PutCommand).callsFake((input) => {
    const item = input.Item as Record<string, unknown> & KeyShape;
    if (input.ConditionExpression?.includes('attribute_not_exists(PK)')) {
      if (store.has(keyOf(item))) throw new ConditionalCheckFailed();
    }
    store.set(keyOf(item), { ...item });
    return {};
  });

  ddbMock.on(GetCommand).callsFake((input) => {
    const key = input.Key as KeyShape;
    return { Item: store.get(keyOf(key)) };
  });

  // General `SET a = :x, b = :y` UpdateCommand support (U7 framing writers + the
  // device-auth status guards). Honours an `attribute_exists(PK)` precondition
  // and the `#s = :guard` conditional-status check; applies each `SET` clause by
  // resolving `:value` placeholders (and `#name` aliases) against the item.
  ddbMock.on(UpdateCommand).callsFake((input) => {
    const key = input.Key as KeyShape;
    const existing = store.get(keyOf(key));
    const values = (input.ExpressionAttributeValues ?? {}) as Record<string, unknown>;
    const names = (input.ExpressionAttributeNames ?? {}) as Record<string, string>;
    const cond = input.ConditionExpression ?? '';

    if (cond.includes('attribute_exists(PK)') && !existing) {
      throw new ConditionalCheckFailed();
    }
    // Conditional status guard of the form `#s = :guard` (device-auth flows).
    const guardMatch = cond.match(/#?(\w+)\s*=\s*(:\w+)/);
    if (existing && guardMatch && cond.includes('#s =')) {
      const attr = names['#s'] ?? 's';
      const expected = values[guardMatch[2] as string];
      if ((existing as Record<string, unknown>)[attr] !== expected) {
        throw new ConditionalCheckFailed();
      }
    }

    const next: Record<string, unknown> = { ...(existing ?? key) };
    const setClause = (input.UpdateExpression ?? '').replace(/^\s*SET\s+/i, '');
    for (const assignment of setClause.split(',')) {
      const m = assignment.trim().match(/^(#?[\w]+)\s*=\s*(:[\w]+)\s*$/);
      if (!m) continue;
      const attr = m[1]!.startsWith('#') ? (names[m[1]!] ?? m[1]!.slice(1)) : m[1]!;
      next[attr] = values[m[2]!];
    }
    store.set(keyOf(key), next);
    return {};
  });

  ddbMock.on(DeleteCommand).callsFake((input) => {
    const key = input.Key as KeyShape;
    store.delete(keyOf(key));
    return {};
  });

  ddbMock.on(QueryCommand).callsFake((input) => {
    const values = (input.ExpressionAttributeValues ?? {}) as Record<string, string>;
    const pk = values[':pk'];
    const skPrefix = values[':sk'];

    // A GSI1 query keys on the GSI1PK/GSI1SK attributes instead of PK/SK. The
    // Repo only ever begins_with(SK)-prefixes the base table, so for GSI1 we
    // match on GSI1PK and sort by GSI1SK.
    const onIndex = input.IndexName !== undefined;
    const partKey = onIndex ? 'GSI1PK' : 'PK';
    const sortKey = onIndex ? 'GSI1SK' : 'SK';

    let items = [...store.values()].filter((it) => {
      if ((it as Record<string, unknown>)[partKey] !== pk) return false;
      if (!onIndex && skPrefix !== undefined) {
        return String((it as KeyShape).SK).startsWith(skPrefix);
      }
      return true;
    });
    // Honour sort-key ordering + ScanIndexForward for event replay / live lists.
    items.sort((a, b) =>
      String((a as Record<string, unknown>)[sortKey] ?? '').localeCompare(
        String((b as Record<string, unknown>)[sortKey] ?? ''),
      ),
    );
    if (input.ScanIndexForward === false) items.reverse();
    if (typeof input.Limit === 'number') items = items.slice(0, input.Limit);
    return { Items: items };
  });

  return store;
}
