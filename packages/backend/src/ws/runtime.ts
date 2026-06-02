import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import {
  ApiGatewayManagementApiClient,
  PostToConnectionCommand,
} from '@aws-sdk/client-apigatewaymanagementapi';
import type { APIGatewayProxyWebsocketEventV2 } from 'aws-lambda';
import { Repo } from '../db/repo.js';

/**
 * Shared runtime wiring for the WebSocket handlers. AWS clients are created
 * lazily and memoised across warm invocations; tests inject their own Repo and
 * poster so nothing here touches the network.
 */

let docClient: DynamoDBDocumentClient | undefined;
let repo: Repo | undefined;

export function defaultRepo(): Repo {
  if (!repo) {
    docClient ??= DynamoDBDocumentClient.from(new DynamoDBClient({}));
    repo = new Repo(docClient);
  }
  return repo;
}

/**
 * Derives the API Gateway Management endpoint for a WebSocket invocation so the
 * backend can post frames back to connected clients (`@connections`).
 */
export function managementEndpoint(event: APIGatewayProxyWebsocketEventV2): string {
  const override = process.env.WS_MANAGEMENT_ENDPOINT;
  if (override) return override;
  const { domainName, stage } = event.requestContext;
  return `https://${domainName}/${stage}`;
}

/**
 * Posts a JSON frame to a connection. Returns `false` if the connection is gone
 * (410 GoneException) so callers can prune stale listeners; rethrows anything
 * else. Abstracted behind an interface so handlers take an injectable poster.
 */
export interface ConnectionPoster {
  post(connectionId: string, body: unknown): Promise<boolean>;
}

export class ApiGwPoster implements ConnectionPoster {
  private readonly client: ApiGatewayManagementApiClient;

  constructor(endpoint: string) {
    this.client = new ApiGatewayManagementApiClient({ endpoint });
  }

  async post(connectionId: string, body: unknown): Promise<boolean> {
    try {
      await this.client.send(
        new PostToConnectionCommand({
          ConnectionId: connectionId,
          Data: new TextEncoder().encode(JSON.stringify(body)),
        }),
      );
      return true;
    } catch (err) {
      if ((err as { name?: string }).name === 'GoneException') return false;
      throw err;
    }
  }
}
