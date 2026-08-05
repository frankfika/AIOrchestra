# Upgrade Runbook

> **Runbook version:** orchestra 0.1.0 (M7) · **Date:** 2026-08-06
>
> M7 supports two upgrade paths: a **dev** path (replace the
> container image) and a **production** path (Helm rolling
> upgrade). The production path is rolling + drain + health
> gate; no downtime is expected.

## 1. Pre-flight

1. Read the release notes for the target version.
2. Confirm the SLO attainment from the current pilot — if
   availability has dropped below the target, **stop** and
   investigate before upgrading.
3. Run the migration dry-run:

   ```bash
   docker compose exec postgres \
     psql -U orchestra -d orchestra -f /opt/migrations/<version>.sql --echo
   ```

4. Snapshot the database.

## 2. Dev upgrade (docker compose)

```bash
git fetch --tags
git checkout v0.<next>  # tag of the new version
docker compose build
docker compose up -d
docker compose logs -f orchestra | grep "Started"
```

## 3. Production upgrade (Helm rolling)

```bash
# 3.1. Apply the Helm chart with the new image tag
helm upgrade orchestra ./deploy/helm \
  --set image.tag=v0.<next> \
  --reuse-values

# 3.2. Watch the rollout
kubectl rollout status deployment/orchestra --timeout=300s

# 3.3. Health gate — the new pod must be ready before the old
#      one is killed. The Helm chart's readinessProbe is
#      /healthz; a 503 on /healthz blocks traffic.
kubectl get pods -l app.kubernetes.io/name=orchestra
```

## 4. Verification

- `orchestra capabilities` returns the same set as before.
- `orchestra benchmark` runs to completion.
- A representative task (`ctr-001`) submitted via the JSON API
  reaches `succeeded` end-to-end.

## 5. Rollback

If the upgrade breaks:

```bash
helm rollback orchestra <previous-revision>
# OR
docker compose down
git checkout v0.<prev>
docker compose up -d
```

The Postgres schema is **forward-only** between minor versions.
A downgrade that needs an older schema requires restoring from
the snapshot taken in step 1.
