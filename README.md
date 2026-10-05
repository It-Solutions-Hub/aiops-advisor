# AIOps Advisor — Lab 2 mock recommendation service

A tiny stdlib-only HTTP service that plays the "AIOps Advisor" role in Lab 2. It
inspects a captured Terraform plan for **CHANGE-4471** and returns a fixed set of
recommendations about the proposed change. It is **not an oracle** — it never says
which recommendations are correct. Telling them apart is the assessed task.

> **Instructor material.** The service package ships on the Lab 2 VM image, but the
> fixture's rationale, the canonical decisions, and this directory's layout are not
> student-facing. Students interact with it only through `aiops-advisor analyze`,
> which `automation/provision_app_tier.sh propose` calls for them.

## Layout

```
aiops-advisor/
  service/
    advisor_service.py            # the HTTP service (:8600)
    fixtures/change-4471.json     # recommendation set for CHANGE-4471
  cli/
    aiops-advisor                 # client CLI -> /usr/local/bin/aiops-advisor
  deploy/
    aiops-advisor.service         # systemd unit
  README.md
```

Canonical accept/correct/reject decisions for the fixture:
[`../instructor/expected-decisions.json`](../instructor/expected-decisions.json).

## Endpoints

| Method | Path       | Purpose |
|--------|------------|---------|
| `GET`  | `/healthz` | liveness; lists known changes and call count |
| `POST` | `/analyze` | returns `{"recommendations": [{id, category, claim, confidence}, ...]}` plus an advisory `meta` block |
| `GET`  | `/history` | full audit trail of `/analyze` calls — used by `evaluate-lab2.sh` |

`/analyze` request body (JSON): `change` (default `CHANGE-4471`), `plan_b64`
(base64 of the plan file bytes), `plan_sha256` (verified if present),
`plan_filename`, `plan_summary` (`{add, change, destroy}` — interpolated into
REC-2's claim).

Every `/analyze` call is appended to the JSONL audit log at
`AIOPS_ADVISOR_LOG` (default `/var/log/aiops-advisor/requests.jsonl`), recording
`request_id`, `served_at`, `plan_sha256`, `plan_bytes`, `plan_recognized`, and the
`recommendation_ids` returned. History survives a restart by reloading this file.

## Environment variables

| Var | Default | Meaning |
|-----|---------|---------|
| `AIOPS_ADVISOR_ADDR` | `0.0.0.0` | bind address |
| `AIOPS_ADVISOR_PORT` | `8600` | bind port |
| `AIOPS_ADVISOR_FIXTURES` | `<service>/fixtures` | fixture directory |
| `AIOPS_ADVISOR_LOG` | `/var/log/aiops-advisor/requests.jsonl` | audit log (falls back to `~/.aiops-advisor/` then cwd if unwritable) |
| `AIOPS_ADVISOR_URL` | `http://127.0.0.1:8600` | CLI: service base URL |

## Install on the VM image

```bash
install -m 0755 -d /opt/aiops-advisor/fixtures /var/log/aiops-advisor
install -m 0644 service/advisor_service.py  /opt/aiops-advisor/
install -m 0644 service/fixtures/*.json     /opt/aiops-advisor/fixtures/
install -m 0755 cli/aiops-advisor           /usr/local/bin/aiops-advisor
useradd --system --no-create-home --shell /usr/sbin/nologin aiops || true
chown -R aiops:aiops /var/log/aiops-advisor
install -m 0644 deploy/aiops-advisor.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now aiops-advisor
```

## Smoke test

```bash
curl -s localhost:8600/healthz | python3 -m json.tool
# -> {"status": "ok", "changes": ["CHANGE-4471"], "calls": N}

# against a real captured plan:
aiops-advisor analyze --plan-file plan/app-tier.plan --out /tmp/recs.json
python3 -m json.tool < /tmp/recs.json          # 6 recommendations REC-1..REC-6
curl -s localhost:8600/history | python3 -m json.tool   # the call just made
```

## The fixture (CHANGE-4471) at a glance

Six recommendations. Full rationale in
[`../instructor/expected-decisions.json`](../instructor/expected-decisions.json).

| id | category | shape | canonical decision |
|----|----------|-------|--------------------|
| REC-1 | `state` | claims 2 instances already run — misstates current state | **correct** |
| REC-2 | `capacity` | plan = exactly 5, 0 changed, 0 destroyed, per spec | **accept** |
| REC-3 | `scope` | add a DB connection pooler — out of ticket scope | **reject** |
| REC-4 | `security` | publish ports 8080-8084 — breaks no-published-ports rule | **reject** |
| REC-5 | `naming` | shorten instance names — breaks the naming convention | **reject** |
| REC-6 | `capacity` | "no memory limit is set" — false; TF already sets 8192 MB | **correct** |

Wrong ones carry high confidence (0.84–0.95); the two that are safe/false-premise
sit lower (0.58, 0.71) — deliberately, to make the point that confidence is not
correctness.
