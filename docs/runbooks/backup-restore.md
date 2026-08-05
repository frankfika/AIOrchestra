# Backup & Restore Runbook

> **Runbook version:** orchestra 0.1.0 (M7) · **Date:** 2026-08-06
>
> The M7 RPO target is **60 seconds**; the M7 RTO target is
> **15 minutes** for a single-region outage and **60 minutes**
> for a multi-region outage.

## 1. Backups

### 1.1. PostgreSQL — continuous WAL + nightly base

```bash
# Nightly base backup (cron)
pg_basebackup -h postgres -U orchestra -D /var/backups/orchestra/base \
  -Ft -z -Xs -P

# WAL archive (continuous)
archive_command = 'cp %p /var/backups/orchestra/wal/%f'
```

### 1.2. SBOM + signed provenance

```bash
python -m orchestra.cli.sbom > /var/backups/orchestra/sbom.json
python -c "from orchestra.enterprise.supply_chain import build_provenance, sign_artifact, ...; \
  p = build_provenance(...); print(p.to_dict())" \
  > /var/backups/orchestra/provenance.json
```

### 1.3. Tenant data

Tenant exports are produced by an admin-scoped CLI:

```bash
orchestra admin export --tenant tenant:acme \
  --output /var/backups/orchestra/tenants/acme-<date>.json
```

## 2. Restore

### 2.1. Single-region

```bash
# 2.1.1. Stop the API
docker compose stop orchestra
# OR
kubectl scale deployment orchestra --replicas=0

# 2.1.2. Restore the base backup + replay WAL
pg_restore -h postgres -U orchestra -d orchestra --clean --if-exists \
  /var/backups/orchestra/base/<date>

# 2.1.3. Apply WAL up to the recovery point
# (PITR: `recovery_target_time = '<target>'` in postgresql.auto.conf)

# 2.1.4. Start the API and run /healthz
docker compose start orchestra
curl http://localhost:8000/healthz
```

### 2.2. Multi-region failover

For an active-active setup, promote the replica in the secondary
region by re-pointing the API's `DATABASE_URL` to the new
primary. The Helm chart's liveness probe ensures the new pod only
receives traffic after `/healthz` returns 200.

## 3. Verification

After every restore:

1. Submit a contract review; verify the result is identical to
   a pre-backup run.
2. Verify the audit timeline is intact (the `Merkle` log in M2
   produces a single root; the post-restore root must match the
   pre-backup root up to the recovery point).
3. Run `orchestra benchmark` to confirm the SLO is back inside
   the target.
