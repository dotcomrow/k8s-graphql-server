# k8s-graphql-server
GraphQL server main entrypoint for external services

## Hasura Configuration
Hasura GraphQL Engine is configured via a generated, fully-annotated env file:

- Source file: `config/hasura/hasura.env`
- Kubernetes ConfigMap: `manifests/hasura-configmap.yaml` (key: `hasura.env`)

The Hasura Deployment (`manifests/hasura.yaml`) mounts this ConfigMap at `/etc/hasura/hasura.env` and sources it before starting `graphql-engine`.

Env vars set directly on the container in `manifests/hasura.yaml` take precedence over values in `/etc/hasura/hasura.env` (the file provides defaults).

`manifests/hasura-catalog-preflight.yaml` runs before the Hasura Deployment (Argo sync-wave ordering) and repairs a known partial-catalog state that causes Hasura startup to fail with:
`Rows returned != 1` from `hdb_catalog.hdb_version`.

The completed preflight Job is intentionally retained in the cluster. If Kubernetes TTL deletes it, ArgoCD treats the missing Job as drift and recreates it continuously.

### Regenerating The Config Template
The template is generated from the exact `hasura/graphql-engine` image pinned in `manifests/hasura.yaml`:

```sh
python3 scripts/hasura/generate_hasura_config.py
```

Notes:
- The generator inventories supported env vars by inspecting the `graphql-engine` binary in the image, so it stays aligned with the version you run.
- Secrets/connection strings are intentionally left commented out because the file is stored in a ConfigMap. This repo currently injects `HASURA_GRAPHQL_DATABASE_URL` and `HASURA_GRAPHQL_ADMIN_SECRET` via Vault in `manifests/hasura.yaml`.
- Redis connection env vars (`HASURA_GRAPHQL_*_REDIS_*`) are configured via Vault injection in `manifests/hasura.yaml` and are intentionally omitted from the generated ConfigMap to avoid accidental override.

## Async Kafka Bridge
This repo includes idempotent bootstrap jobs for the async GraphQL + Kafka flow:

- `manifests/graphql-async-bootstrap.yaml`
  - Creates/updates table `graphql.client_async_messages` in the Hasura DB.
  - Tracks the table in Hasura metadata.
  - Creates Hasura permissions for roles `user` and `service`.
- `manifests/graphql-kafka-setup.yaml`
  - Inspects existing Kafka state.
  - Creates/updates async topics:
    - `graphql.async.requests.v1`
    - `graphql.async.responses.v1`
    - `graphql.async.responses.dlq.v1`
  - Uses Kafka credentials from Vault paths:
    - `secret/data/graphql-kafka-async-username` (`value`)
    - `secret/data/graphql-kafka-async-password` (`value`)
  - Authenticates to Kafka with `SASL_PLAINTEXT` + `SCRAM-SHA-256` on `kafka.kafka.svc.internal.lan:9092`.
  - Applies ACLs if Kafka authorizer is enabled (skips ACL creation when broker security/authorizer is disabled).
- `manifests/graphql-async-response-writer.yaml`
  - Runs a long-lived consumer (`Deployment`) on topic `graphql.async.responses.v1`.
  - Authenticates to Kafka using the same Vault-managed async principal.
  - Writes service responses idempotently into `graphql.client_async_messages` by `request_id`.
  - Sends permanently invalid response messages (for example invalid JSON/missing `request_id`) to `graphql.async.responses.dlq.v1`.
- `manifests/graphql-async-request-publisher.yaml`
  - Runs a Hasura Action handler (`Deployment` + `Service`) for generic async request publishing.
  - Handles action `publish_async_request`.
  - Atomically inserts a queued operation and its transactional outbox record.
  - Handles lifecycle actions `cancel_async_operation` and `retry_async_operation`.
- `manifests/graphql-operation-outbox-relay.yaml`
  - Runs two relay replicas using `FOR UPDATE SKIP LOCKED` claims.
  - Publishes committed outbox envelopes to Kafka with at-least-once delivery.
  - Applies bounded exponential retry and marks exhausted publication attempts dead-lettered.
- `manifests/graphql-operation-test-worker.yaml`
  - Provides the generic test handler `platform-test-worker`.
  - Supports `echo`, `delay`, and `fail` for end-to-end platform validation.
  - Uses a dedicated read-only database identity to cooperatively acknowledge cancellation during delayed work.
  - Publishes progress and terminal responses without application-specific behavior.
- `manifests/graphql-async-cleanup.yaml`
  - Runs a scheduled cleanup (`CronJob`) every 10 minutes.
  - Marks overdue nonterminal operations `expired` in bounded batches.
  - Preserves terminal records so clients can restore authoritative results after reconnecting.

Vault role/policy for the Kafka setup job are created by:
- `manifests/00-vault-yugabyte-init.yaml`

### Verify async bootstrap
```sh
kubectl -n graphql get job graphql-async-bootstrap graphql-kafka-setup
kubectl -n graphql get deploy graphql-async-response-writer
kubectl -n graphql get deploy graphql-async-request-publisher
kubectl -n graphql get deploy graphql-operation-outbox-relay graphql-operation-test-worker
kubectl -n graphql get cronjob graphql-async-cleanup
kubectl -n graphql logs job/graphql-async-bootstrap --tail=200
kubectl -n graphql logs job/graphql-kafka-setup --tail=200
kubectl -n graphql logs deploy/graphql-async-response-writer --tail=200
kubectl -n graphql logs deploy/graphql-async-request-publisher --tail=200
kubectl -n graphql logs deploy/graphql-operation-outbox-relay --tail=200
kubectl -n graphql logs deploy/graphql-operation-test-worker --tail=200
kubectl -n graphql get job --sort-by=.metadata.creationTimestamp | tail -n 5
```

### Generic async action
Bootstrap creates Hasura action `publish_async_request` with handler:

- `http://graphql-async-request-publisher.graphql.svc.cluster.local:8080/action/publish_async_request`

Action argument:
- `input: json!`

Recommended action input shape:

```json
{
  "request_id": "optional-idempotency-key",
  "client_id": "optional-for-service-role",
  "handler": "billing-worker",
  "operation": "invoice.create",
  "payload": { "invoice_id": "inv_123", "amount": 42.5 },
  "metadata": { "tenant": "acme" },
  "priority": "normal",
  "expires_in_seconds": 3600
}
```

The ingress acknowledges only after both the operation and outbox message are committed in one
database transaction. Kafka delivery happens asynchronously, so `accepted` never means that a
worker has started or completed the operation.

Workers must use a unique consumer group for each handler implementation, use `request_id` as the
idempotency key, and publish results to the declared response topic. Replicas of one handler share
that handler's group; different handlers must not share a group.

### Generic operation lifecycle

Supported lifecycle states are `accepted`, `queued`, `scheduled`, `leased`, `running`,
`retry-scheduled`, `cancel-requested`, `succeeded`, `failed`, `cancelled`, `expired`, and
`dead-lettered`.

```graphql
mutation CancelOperation($input: json!) {
  cancel_async_operation(input: $input)
}

mutation RetryOperation($input: json!) {
  retry_async_operation(input: $input)
}
```

Both actions accept `{ "operation_id": "..." }` and enforce ownership from the Hasura session.
Cancellation after a worker starts is cooperative; a worker must implement that check before an
MFE advertises the operation as cancelable.

The platform test worker demonstrates that requirement by polling only the authoritative operation
status with the dedicated `graphql_operation_test_worker_ro` database credential while delayed work
is active. That role can select only `request_id`, `status`, and `cancel_requested_at` from the
operation table. Production workers may use another cooperative cancellation transport, but a late
result must never overwrite an acknowledged cancellation. Request and response Kafka transport
coordinates are stored in separate columns and are not exposed through the application-user Hasura
role, so transport details remain an infrastructure concern.

Execution expiry and historical retention are deliberately separate. The expiry job marks state
but does not delete terminal records. A retention policy must preserve results long enough for
suspended PWAs to restore them and should be introduced only with an explicitly approved window.

### Generic test handler

The platform test worker accepts this route:

```json
{
  "handler": "platform-test-worker",
  "operation": "echo",
  "payload": { "message": "hello" },
  "metadata": {
    "operation_type": "platform.test.echo",
    "operation_version": "1.0.0",
    "max_attempts": 3
  },
  "priority": "normal"
}
```

It also supports `delay` with `delay_seconds` capped at 30 seconds and `fail` for testing failure
delivery.

### Platform operation handler

`graphql-platform-operation-worker` is the allowlisted reference adapter for durable cross-service
commands. It consumes `graphql.async.requests.v1` as consumer group
`graphql-operation-platform-worker-v1`, accepts only handler `platform-operation-worker`, and
publishes progress and terminal results to `graphql.async.responses.v1`.

Supported routes are:

- `platform.app.deploy` and `platform.app.destroy`, which queue the existing platform-deploy
  workflow and return its authoritative downstream operation identifier;
- `platform.notification.queue`, which validates and queues a request into the existing
  channel-specific notification Kafka flow;
- `platform.runtime-canary.run`, which executes one named runtime canary and returns its run and
  step detail.

The worker is deliberately not a generic HTTP proxy. Every route has a fixed target and payload
adapter, uses a dedicated Vault-injected service credential, and carries no end-user token on
Kafka. Add new platform commands as explicit routes with manifest schemas and Gravitee async-role
policy rather than accepting caller-provided URLs.

### Async Route Authorization (Annotation-Driven)

`publish_async_request` remains a generic carrier. Route-level authorization is enforced by
`graphql-async-request-publisher` via `graphql-gravitee-sync` policy lookup:

- `handler` + `operation` from action input
- `x-hasura-role` from session variables
- optional `required_role` / `requiredRole` from action input or metadata

When `required_role` is provided (for example from Directus MFE `requiredRole` config), the API
enforces all of the following before publishing to Kafka:

- caller has that role in `x-hasura-allowed-roles` (or it is the active `x-hasura-role`)
- Gravitee async policy for the handler/operation also allows that exact role

Define allowed roles on the Gravitee API labels (via service annotation
`gravitee.io/definition-labels`):

- `hasura.async.handler=<handler-name-used-by-mfe>`
- `hasura.action.roles=<role1|role2|...>` for default handler roles
- `hasura.async.operation.roles.<operation>=<role1|role2|...>` for operation-specific overrides

Example:

```yaml
metadata:
  annotations:
    gravitee.io/definition-labels: "internal,hasura,hasura.async.handler=billing-worker,hasura.action.roles=billing_user"
```

Operation-specific example:

```yaml
metadata:
  annotations:
    gravitee.io/definition-labels: "internal,hasura,hasura.async.handler=billing-worker,hasura.action.roles=user|service,hasura.async.operation.roles.invoice.create=billing_user"
```

Published Kafka request envelope (`graphql.async.requests.v1`):

```json
{
  "spec_version": "async.request.v1",
  "request_id": "req-123",
  "client_id": "user-123",
  "route": { "handler": "billing-worker", "operation": "invoice.create" },
  "payload": { "invoice_id": "inv_123", "amount": 42.5 },
  "options": {
    "priority": "normal",
    "reply_topic": "graphql.async.responses.v1",
    "expires_at": "2026-03-07T23:50:00Z"
  },
  "metadata": { "tenant": "acme" },
  "submitted_at": "2026-03-07T22:50:00Z"
}
```

GraphQL mutation example:

```graphql
mutation PublishAsync($input: json!) {
  publish_async_request(input: $input)
}
```

Verify Hasura now exposes subscriptions (subscription root becomes non-null once a table is tracked):
```sh
kubectl -n graphql exec deploy/graphql-gravitee-sync -c sync -- python3 -c \
'import json,urllib.request;admin=open(\"/vault/secrets/hasura-admin\").read().strip();q={\"query\":\"query{ __schema { subscriptionType { name } } }\"};req=urllib.request.Request(\"http://hasura.graphql.svc.cluster.local:8080/v1/graphql\",data=json.dumps(q).encode(),headers={\"Content-Type\":\"application/json\",\"X-Hasura-Admin-Secret\":admin},method=\"POST\");print(urllib.request.urlopen(req,timeout=20).read().decode())'
```

Verify topics on broker:
```sh
kubectl -n kafka exec kafka-0 -- /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka.kafka.svc.internal.lan:9092 --list | sort
```

## Cloudflare Tunnel
This repo includes a Cloudflare Tunnel deployment at `manifests/cloudflare-tunnel.yaml`.
It runs `cloudflared` in the `graphql` namespace and reads `TUNNEL_TOKEN` from a Kubernetes Secret named `cloudflare-tunnel-token`.
That Secret is created by External Secrets using:
- `manifests/cloudflare-tunnel-secretstore.yaml`
- `manifests/cloudflare-tunnel-externalsecret.yaml`

Vault policy + role for External Secrets (`externalsecrets-graphql`) are created by:
- `manifests/00-vault-yugabyte-init.yaml`

### One-time setup
1. Create a named tunnel in Cloudflare Zero Trust (or with the CLI) and copy the tunnel token.
   CLI example:

```sh
cloudflared tunnel login
cloudflared tunnel create graphql
cloudflared tunnel route dns graphql cf-suncoast-graphql-proxy.dev.suncoast.systems
cloudflared tunnel token graphql
```

2. Write the token into Vault (KVv2) at `secret/data/hasura-cloudflare-tunnel-token` with key `value`.
   Example:

```sh
vault kv put secret/hasura-cloudflare-tunnel-token value='<PASTE_TUNNEL_TOKEN>'
```

3. Deploy these manifests via ArgoCD sync (do not apply manually):
- `manifests/00-vault-yugabyte-init.yaml`
- `manifests/cloudflare-tunnel-secretstore-sa.yaml`
- `manifests/cloudflare-tunnel-secretstore.yaml`
- `manifests/cloudflare-tunnel-externalsecret.yaml`
- `manifests/cloudflare-tunnel.yaml`

Argo sync order is set with sync-wave annotations so Vault role/policy and SecretStore are ready before the tunnel deployment.

4. In Cloudflare Zero Trust, add public hostname routing for the tunnel:
   - Example hostname: `cf-suncoast-graphql-proxy.dev.suncoast.systems`
   - Service URL: `http://hasura.graphql.svc.cluster.local:8080`
   - Restrict the public route to `/v1/graphql` and `/v1/graphql/*`.
   - Keep WebSocket upgrades enabled for this hostname/path (required for GraphQL subscriptions).
   - Disable caching on the GraphQL path.

No APISIX route is required for public Hasura ingress in this setup; Cloudflare Tunnel is the only external entrypoint.

### Origin header/logging context
When traffic comes through Cloudflare Tunnel, client/proxy context should come from Cloudflare headers (for example `CF-Connecting-IP`, `CF-Ray`) plus forwarded headers (`X-Forwarded-For`, `X-Request-Id`).

`manifests/gravitee-hasura-sync.yaml` maps these headers into action upstream requests and logs them, so action logs keep client IP/request correlation without APISIX.

### Verify
```sh
kubectl -n graphql get deploy,pod -l app=cloudflared
kubectl -n graphql logs deploy/cloudflared --tail=100 -f
```

### ExternalSecret troubleshooting
If `kubectl -n graphql get externalsecret cloudflare-tunnel-token` shows `SecretSyncedError`, inspect details:

```sh
kubectl -n graphql describe externalsecret cloudflare-tunnel-token
```

A Vault `403 permission denied` means Vault role/policy was not yet applied or is out of date. Ensure Argo has synced `manifests/00-vault-yugabyte-init.yaml` and the role `externalsecrets-graphql` has read/list on:
- `secret/data/hasura-cloudflare-tunnel-token`
- `secret/metadata/hasura-cloudflare-tunnel-token`

### Rotate token
Update Vault and let External Secrets refresh:

```sh
vault kv put secret/hasura-cloudflare-tunnel-token value='<NEW_TUNNEL_TOKEN>'
```

Optional immediate rollout after token update:

```sh
kubectl -n graphql rollout restart deploy/cloudflared
```
