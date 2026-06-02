import type { APIGatewayProxyResultV2, APIGatewayProxyWebsocketEventV2 } from 'aws-lambda';

/**
 * WebSocket `$default` route (U5). Catches any frame whose `action` does not
 * match a declared route. We acknowledge with 200 rather than erroring so a
 * stray/unknown message never tears down an otherwise healthy connection.
 */
export const handler = async (
  _event: APIGatewayProxyWebsocketEventV2,
): Promise<APIGatewayProxyResultV2> => {
  return { statusCode: 200, body: 'ok' };
};
