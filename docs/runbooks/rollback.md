# Rollback Runbook

> **Runbook version:** orchestra 0.1.0 (M7) · **Date:** 2026-08-06
>
> Rollback is a "first-class" action: the upgrade Runbook
> includes the rollback path. This document covers the cases
> the upgrade path does not: kill-switch, plan-amendment
> rollback, and credential rotation.

## 1. Kill Switch (M5 PUB-003)

Trip the publish-side kill switch to refuse all partner calls
within the bounded window:

```python
from orchestra.publishing.kill_switch import KillSwitch
ks = KillSwitch(max_effect_seconds=5.0)
ks.trip(reason="incident-42")
```

`admit()` is denied from the next call forward. Reset with
`ks.reset()` once the incident is resolved.

## 2. Plan Amendment (M1)

A Plan Amendment is the only way to revise a Plan mid-flight. To
roll back a bad amendment:

```python
coordinator.reject_amendment(task_run_id, amendment_id, decided_by="ops", rationale="policy regression")
```

The amendment is removed from the Plan; downstream nodes see
the previous routing decision. The audit timeline records the
rejection so the operator can prove the rollback to a customer.

## 3. Credential rotation (M2)

The Credential Broker per-Node keys can be rotated without
restarting the API. The rotation is in-band; old keys are
retained for the grace period so in-flight Tasks complete.

```python
from orchestra.runtime.credential_broker import CredentialBroker
broker = CredentialBroker(store, lease_ttl_s=900)
broker.rotate_node_grant(task_run_id=task, node_id="public_research", decided_by="ops")
```

## 4. Schema rollback (M6)

The M6 migration is **forward-only** between minor versions.
A schema rollback requires restoring from the pre-upgrade
Postgres snapshot. See `docs/runbooks/backup-restore.md`.
