# k8s-graphql-server
GraphQL server main entrypoint for external services

## Hasura Configuration
Hasura GraphQL Engine is configured via a generated, fully-annotated env file:

- Source file: `config/hasura/hasura.env`
- Kubernetes ConfigMap: `manifests/hasura-configmap.yaml` (key: `hasura.env`)

The Hasura Deployment (`manifests/hasura.yaml`) mounts this ConfigMap at `/etc/hasura/hasura.env` and sources it before starting `graphql-engine`.

Env vars set directly on the container in `manifests/hasura.yaml` take precedence over values in `/etc/hasura/hasura.env` (the file provides defaults).

### Regenerating The Config Template
The template is generated from the exact `hasura/graphql-engine` image pinned in `manifests/hasura.yaml`:

```sh
python3 scripts/hasura/generate_hasura_config.py
```

Notes:
- The generator inventories supported env vars by inspecting the `graphql-engine` binary in the image, so it stays aligned with the version you run.
- Secrets/connection strings are intentionally left commented out because the file is stored in a ConfigMap. This repo currently injects `HASURA_GRAPHQL_DATABASE_URL` and `HASURA_GRAPHQL_ADMIN_SECRET` via Vault in `manifests/hasura.yaml`.
- Redis connection env vars (`HASURA_GRAPHQL_*_REDIS_*`) are configured via Vault injection in `manifests/hasura.yaml` and are intentionally omitted from the generated ConfigMap to avoid accidental override.

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

2. Write the token into Vault (KVv2) at `secret/data/cloudflare-tunnel-token` with key `vaule`.
   Example:

```sh
vault kv put secret/cloudflare-tunnel-token vaule='<PASTE_TUNNEL_TOKEN>'
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
- `secret/data/cloudflare-tunnel-token`
- `secret/metadata/cloudflare-tunnel-token`

### Rotate token
Update Vault and let External Secrets refresh:

```sh
vault kv put secret/cloudflare-tunnel-token vaule='<NEW_TUNNEL_TOKEN>'
```

Optional immediate rollout after token update:

```sh
kubectl -n graphql rollout restart deploy/cloudflared
```
