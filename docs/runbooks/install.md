# Install Runbook

> **Runbook version:** orchestra 0.1.0 (M7) · **Date:** 2026-08-06
>
> This runbook is the canonical install path for the M7 GA
> candidate. A new contributor or operator can reproduce a
> working cluster from a fresh checkout in under 15 minutes
> using only the steps below.

## 1. Prerequisites

- Linux or macOS with `bash`, `git`, `python3.11+`, `docker`, `docker compose`.
- 2 vCPU / 4 GiB RAM minimum (4 vCPU / 8 GiB recommended for production).
- A reachable PostgreSQL 14+ instance (the docker-compose file
  bundles a `postgres:16-alpine` for dev; production should
  point at RDS / Cloud SQL / Crunchy).

## 2. Clean-room install (fresh machine)

```bash
# 2.1. Clone
git clone https://github.com/frankfika/AIOrchestra
cd AIOrchestra

# 2.2. Verify the SBOM
python -c "from orchestra.enterprise.supply_chain import build_sbom_from_pyproject; \
  from pathlib import Path; \
  sbom = build_sbom_from_pyproject(pyproject_path=Path('pyproject.toml')); \
  print(f'{len(sbom.components)} components')"

# 2.3. Build the image
docker build -t orchestra:dev .

# 2.4. Boot the cluster
docker compose up -d

# 2.5. Wait for healthy
docker compose ps
# Expect: postgres "healthy", orchestra "healthy".

# 2.6. Smoke test
docker compose exec orchestra python -m orchestra.cli --base http://localhost:8000 capabilities
# Expect: a JSON list of 5 capabilities.
```

## 3. Helm install (production-shaped)

```bash
# 3.1. Add the local chart
helm install orchestra ./deploy/helm \
  --set image.repository=orchestra \
  --set image.tag=dev \
  --set postgres.host=postgres.example.internal \
  --set postgres.existingSecret=orchestra-postgres \
  --set postgres.existingSecretKey=password

# 3.2. Wait for the rollout
kubectl rollout status deployment/orchestra --timeout=120s

# 3.3. Forward and smoke
kubectl port-forward svc/orchestra 8000:8000
curl -s http://localhost:8000/healthz
# Expect: {"status":"ok", ...}
```

## 4. Verification matrix

| Check              | Command                                          | Pass criterion                       |
| ------------------ | ------------------------------------------------ | ------------------------------------ |
| API health         | `curl /healthz`                                  | `200 OK`                             |
| Capabilities       | `orchestra capabilities`                         | 5 capabilities                        |
| Benchmark          | `orchestra benchmark`                            | 3 baselines run + Pareto verdict      |
| Multi-tenant       | Insert row as tenant A; verify tenant B can't see | B's `get_task_run` returns `None`    |
| Sign / verify      | `python -c "from orchestra.enterprise..."`        | `verify_artifact(...) == True`        |
| Demo Console       | Open `/` in a browser                             | Form renders, submit returns 303     |
