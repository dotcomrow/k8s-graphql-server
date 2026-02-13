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

### One-time setup
1. Create a named tunnel in Cloudflare Zero Trust (or with the CLI) and copy the tunnel token.
   CLI example:

```sh
cloudflared tunnel login
cloudflared tunnel create graphql
cloudflared tunnel route dns graphql cf-suncoast-graphql-proxy.dev.suncoast.systems
cloudflared tunnel token graphql
```

2. Create/update the Kubernetes Secret:

```sh
kubectl -n graphql create secret generic cloudflare-tunnel-token \
  --from-literal=token='<PASTE_TUNNEL_TOKEN>' \
  --dry-run=client -o yaml | kubectl apply -f -
```

3. In Cloudflare Zero Trust, add public hostname routing for the tunnel:
   - Example hostname: `cf-suncoast-graphql-proxy.dev.suncoast.systems`
   - Service URL: `http://hasura.graphql.svc.cluster.local:8080`
4. Deploy tunnel pods:

```sh
kubectl apply -f manifests/cloudflare-tunnel.yaml
```

### Verify
```sh
kubectl -n graphql get deploy,pod -l app=cloudflared
kubectl -n graphql logs deploy/cloudflared --tail=100 -f
```

### Rotate token
Update `cloudflare-tunnel-token` with a new token and restart the deployment:

```sh
kubectl -n graphql rollout restart deploy/cloudflared
```
