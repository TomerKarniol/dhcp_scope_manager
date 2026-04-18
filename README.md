# DHCP Backend With Crossplane

A production-oriented FastAPI service for managing Windows DHCP IPv4 scopes through PowerShell, designed for GitOps reconciliation with Crossplane `provider-http`.

## What This Project Does

This repository connects declarative cluster configuration to real DHCP server state:

1. A values file in Git defines the desired DHCP scope configuration.
2. Helm renders a Crossplane `Request` resource from those values.
3. Crossplane reconciles by calling this API (`GET` / `POST` / `PUT` / `DELETE`).
4. The API executes Windows DHCP PowerShell cmdlets.
5. Current DHCP state is normalized back into a canonical shape so GET equals the desired PUT body.

The user only ever edits values files. The backend is not a manual entry point.

## Architecture

```text
Git (values files — desired state)
  → Helm (renders Crossplane Request CR)
    → Crossplane provider-http (reconciliation engine)
      → FastAPI DHCP backend (validate + normalize + execute)
        → PowerShell cmdlets
          → Windows DHCP Server
```

## Repository Layout

```text
app/
  main.py                    FastAPI app bootstrap
  config.py                  Env-based settings (DHCP_API_TOKEN, HOST, PORT, LOG_LEVEL)
  logging_config.py          JSON structured logging
  exception_handlers.py      Global exception → HTTP mapping (PowerShellError, DhcpEnvironmentError)
  dependencies/
    auth.py                  Bearer token verification (verify_token dependency)
    scopes.py                scope_id and request body validation (validate_scope_id, validate_scope_request)
  models/
    __init__.py              Re-exports DhcpScopePayload, DhcpFailover, DhcpExclusion
    scope.py                 DhcpScopePayload — canonical request/response model
    failover.py              DhcpFailover — failover relationship configuration
    exclusion.py             DhcpExclusion — exclusion range
  routers/
    scopes.py                DHCP scope endpoints (POST/GET/PUT/DELETE /api/v1/scopes/{scope_id})
    health.py                /healthz runtime capability check
  services/
    dhcp_env.py              Runtime guard (OS / PowerShell / DHCP cmdlets check)
    ps_executor.py           PowerShell command runner with error handling
    ps_parsers.py            Parse and normalize PowerShell JSON output
    scope_service.py         Core scope lifecycle logic (create / get / update / delete)
  utils/
    decorators.py            Route-level decorators: handle_http_errors, handle_health_errors, log_call
    ip_utils.py              IP integer conversion and TimeSpan parsing helpers

helm/hosted-cluster-integration/
  Chart.yaml
  values.yaml                Reference values file with all supported fields documented
  templates/
    dhcp-scope-request.yaml  Crossplane Request CR — all verbs (POST/GET/PUT/DELETE) on /{network}
    _dhcp-helpers.tpl        Canonical payload rendering with required-field enforcement

scripts/
  validate_dhcp_values.py    Self-contained Pydantic validator — call with one or more values files
  validate_changed_clusters.py  CI entry point — discovers changed cluster files via git diff,
                                resolves full merge chain, calls validate_dhcp_values.py for each
  requirements.txt           Minimal CI dependencies (pydantic, PyYAML)

tests/
  conftest.py
  test_endpoints.py          HTTP endpoint contracts and status codes
  test_models.py             Pydantic field ordering and serialization
  test_validation.py         IP validation, subnet consistency, failover mode enforcement
  test_parsers.py            PowerShell output parsing and normalization
  test_diff.py               Diff-based update logic
  test_dhcp_env.py           Runtime environment guard behavior
  test_parity.py             GET/PUT parity — the main guard against Crossplane reconciliation loops
  test_edge_cases.py         Edge cases and boundary conditions
```

## Runtime Requirements

The API itself requires:

- Python 3.12+
- Windows host (native Windows, **not** Linux / macOS / WSL)
- `powershell.exe` on PATH
- DHCP PowerShell cmdlets (`Get-DhcpServerv4Scope`, etc.)
  - Windows Server: DHCP Server role or RSAT DHCP tools
  - Windows client: RSAT DHCP tools

The CI validation scripts require only Python 3.12+ and can run on any OS.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

| Variable         | Default   | Description                                                   |
| ---------------- | --------- | ------------------------------------------------------------- |
| `DHCP_API_TOKEN` | _(empty)_ | Bearer token for auth. When unset, auth is disabled entirely. |
| `HOST`           | `0.0.0.0` | Bind address                                                  |
| `PORT`           | `8080`    | Bind port                                                     |
| `LOG_LEVEL`      | `INFO`    | Log level                                                     |

A `.env` file in the repo root is also supported.

## Run the API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## API Endpoints

Base path: `/api/v1`

`scope_id` is always the IPv4 network address of the scope (e.g. `10.20.30.0`).

All `/api/v1/scopes*` endpoints share two implicit checks that run before the handler:

- **Environment guard** — rejects requests when the host cannot execute DHCP automation (wrong OS, missing PowerShell, no DHCP cmdlets). Returns `503`.
- **Auth** — rejects requests when `DHCP_API_TOKEN` is set and the token is missing or wrong. Returns `401`.

---

### `GET /api/v1/scopes`

Returns all scopes sorted by network address (ascending).

| Status | Body                                                         | When                            |
| ------ | ------------------------------------------------------------ | ------------------------------- |
| `200`  | `[DhcpScopePayload, ...]`                                    | Success                         |
| `401`  | `{"detail": "Unauthorized"}`                                 | Bad or missing bearer token     |
| `500`  | `{"detail": "PowerShell command failed", "ps_error": "..."}` | PowerShell cmdlet failed        |
| `503`  | `{"detail": "...", "reason": "..."}`                         | Host cannot run DHCP automation |

---

### `POST /api/v1/scopes/{scope_id}`

Creates the scope if it does not exist, then converges all options, exclusions, and failover to the desired state. Idempotent — never fails if the scope already exists.

| Status | Body                                                         | When                                                                        |
| ------ | ------------------------------------------------------------ | --------------------------------------------------------------------------- |
| `200`  | `DhcpScopePayload`                                           | Scope created or already present and converged                              |
| `400`  | `{"detail": "..."}`                                          | `scope_id` is not a valid IPv4 address, or path `scope_id` ≠ body `network` |
| `401`  | `{"detail": "Unauthorized"}`                                 | Bad or missing bearer token                                                 |
| `422`  | FastAPI validation error                                     | Request body fails Pydantic field constraints                               |
| `500`  | `{"detail": "PowerShell command failed", "ps_error": "..."}` | PowerShell cmdlet failed                                                    |
| `503`  | `{"detail": "...", "reason": "..."}`                         | Host cannot run DHCP automation                                             |

---

### `GET /api/v1/scopes/{scope_id}`

Returns the current canonical state of the scope. When Crossplane sees a `404` here it issues `POST` to create the scope.

| Status | Body                                                         | When                                                       |
| ------ | ------------------------------------------------------------ | ---------------------------------------------------------- |
| `200`  | `DhcpScopePayload`                                           | Scope found                                                |
| `400`  | `{"detail": "..."}`                                          | `scope_id` is not a valid IPv4 address                     |
| `401`  | `{"detail": "Unauthorized"}`                                 | Bad or missing bearer token                                |
| `404`  | `{"detail": "Scope {scope_id} not found"}`                   | Scope does not exist on the DHCP server                    |
| `500`  | `{"detail": "PowerShell command failed", "ps_error": "..."}` | PowerShell cmdlet failed for a reason other than not-found |
| `503`  | `{"detail": "...", "reason": "..."}`                         | Host cannot run DHCP automation                            |

---

### `PUT /api/v1/scopes/{scope_id}`

Diff-based convergence — compares the current scope state to the desired payload and issues only the PowerShell cmdlets needed to reconcile the difference.

| Changed fields                                                            | PowerShell cmdlet                                                                            |
| ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `scopeName`, `leaseDurationDays`, `description`, `startRange`, `endRange` | `Set-DhcpServerv4Scope`                                                                      |
| `gateway`, `dnsServers`, `dnsDomain`                                      | `Set-DhcpServerv4OptionValue`                                                                |
| Exclusions added                                                          | `Add-DhcpServerv4ExclusionRange`                                                             |
| Exclusions removed                                                        | `Remove-DhcpServerv4ExclusionRange`                                                          |
| Failover added / changed / removed                                        | `Add-DhcpServerv4Failover` / `Set-DhcpServerv4Failover` / `Remove-DhcpServerv4FailoverScope` |

| Status | Body                                                         | When                                                                        |
| ------ | ------------------------------------------------------------ | --------------------------------------------------------------------------- |
| `200`  | `DhcpScopePayload`                                           | Scope updated (or already at desired state — no-op)                         |
| `400`  | `{"detail": "..."}`                                          | `scope_id` is not a valid IPv4 address, or path `scope_id` ≠ body `network` |
| `401`  | `{"detail": "Unauthorized"}`                                 | Bad or missing bearer token                                                 |
| `404`  | `{"detail": "Scope {scope_id} not found"}`                   | Scope does not exist — Crossplane responds by issuing `POST`                |
| `422`  | FastAPI validation error                                     | Request body fails Pydantic field constraints                               |
| `500`  | `{"detail": "PowerShell command failed", "ps_error": "..."}` | PowerShell cmdlet failed                                                    |
| `503`  | `{"detail": "...", "reason": "..."}`                         | Host cannot run DHCP automation                                             |

---

### `DELETE /api/v1/scopes/{scope_id}`

Deletes the scope and cleans up its failover relationship and exclusion ranges. Idempotent — returns `204` even if the scope does not exist.

Deletion order:

1. Remove scope from failover relationship (`Remove-DhcpServerv4FailoverScope`)
2. Remove failover relationship if now empty (`Remove-DhcpServerv4Failover`)
3. Remove each exclusion range
4. Remove the scope (`Remove-DhcpServerv4Scope -Force`)

If failover detach fails, the delete propagates a `500` so Crossplane retries on the next cycle rather than removing the CR while the scope remains on the server.

| Status | Body                                                         | When                                                   |
| ------ | ------------------------------------------------------------ | ------------------------------------------------------ |
| `204`  | _(empty)_                                                    | Scope deleted, or scope did not exist                  |
| `400`  | `{"detail": "..."}`                                          | `scope_id` is not a valid IPv4 address                 |
| `401`  | `{"detail": "Unauthorized"}`                                 | Bad or missing bearer token                            |
| `500`  | `{"detail": "PowerShell command failed", "ps_error": "..."}` | PowerShell cmdlet failed (e.g. failover detach failed) |
| `503`  | `{"detail": "...", "reason": "..."}`                         | Host cannot run DHCP automation                        |

---

### `GET /healthz`

Checks that the runtime environment can execute DHCP automation:

1. Native Windows OS (not WSL / Linux / macOS)
2. `powershell.exe` present and executable
3. DHCP cmdlets available (`Get-DhcpServerv4Scope` discoverable)

Protected by auth (like all endpoints). Does **not** check the DHCP environment dependency before running — it is the check itself, so it always returns a structured response rather than a plain 503.

| Status | Body                                                    | When                        |
| ------ | ------------------------------------------------------- | --------------------------- |
| `200`  | `{"status": "ok"}`                                      | All checks pass             |
| `401`  | `{"detail": "Unauthorized"}`                            | Bad or missing bearer token |
| `503`  | `{"status": "error", "detail": "...", "reason": "..."}` | Any runtime check fails     |

`reason` values: `unsupported_os`, `wsl_detected`, `powershell_not_found`, `powershell_exec_failed`, `dhcp_cmdlets_unavailable`.

## Canonical Payload Shape

```json
{
  "scopeName": "cluster-a-workers",
  "network": "10.20.30.0",
  "subnetMask": "255.255.255.0",
  "startRange": "10.20.30.50",
  "endRange": "10.20.30.200",
  "leaseDurationDays": 8,
  "description": "",
  "gateway": "10.20.30.1",
  "dnsServers": ["10.10.1.5", "10.10.1.6"],
  "dnsDomain": "lab.local",
  "exclusions": [{ "startAddress": "10.20.30.1", "endAddress": "10.20.30.10" }],
  "failover": null
}
```

- Field order is intentional and tested — Crossplane byte-compares GET response to PUT body.
- `failover` is either `null` or a full failover object (no partial objects).
- Exclusions are always returned sorted by IP (ascending). Values files must match this order.
- DNS server order is preserved exactly (primary/secondary semantics — never sorted).
- `description` defaults to `""` (never `null`).

## Failover Model

Supported modes: `HotStandby`, `LoadBalance`

| Mode          | Required fields      | Normalized fields                                 |
| ------------- | -------------------- | ------------------------------------------------- |
| `HotStandby`  | `serverRole`         | `loadBalancePercent` → `0`                        |
| `LoadBalance` | `loadBalancePercent` | `serverRole` → `"Active"`, `reservePercent` → `0` |

Normalization at both the Helm template layer and the Pydantic model layer prevents GET/PUT drift
when values include cross-mode fields.

## Helm Chart

The chart under `helm/hosted-cluster-integration` renders a single Crossplane `Request` CR.

Key behaviors:

- **Crossplane object name** is based only on `dhcp_values.network` (`dhcp-scope-10-20-30-0`).
  Changing `scopeName` does **not** create a new Crossplane CR or delete the live scope.
- **Required fields** — `helm template` fails with a clear error if any of these are missing:
  `dhcp_values.network`, `dhcp_values.scopeName`, `dhcp_values.subnetMask`, `dhcp_values.startRange`,
  `dhcp_values.endRange`, `dhcp_values.leaseDurationDays`, `dhcp_values.gateway`,
  `dhcp_values.dns.servers`, `dhcp_values.dns.domain`, `apiServer.url`
- **`providerConfigRef.name`** is configurable via `crossplane.providerConfigName`
  (defaults to `dhcp-http`).

```bash
helm template dhcp-request ./helm/hosted-cluster-integration \
  -f ./helm/hosted-cluster-integration/values.yaml
```

## HTTP Response Codes

Quick reference — see the per-endpoint tables above for the exact set each route can return.

| Code  | Meaning               | Body shape                                                                                                     |
| ----- | --------------------- | -------------------------------------------------------------------------------------------------------------- |
| `200` | OK                    | `DhcpScopePayload` or `[DhcpScopePayload, ...]` or `{"status": "ok"}`                                          |
| `204` | No Content            | _(empty)_ — DELETE only                                                                                        |
| `400` | Bad Request           | `{"detail": "..."}` — invalid `scope_id` or path/body network mismatch                                         |
| `401` | Unauthorized          | `{"detail": "Unauthorized"}` — only when `DHCP_API_TOKEN` is set                                               |
| `404` | Not Found             | `{"detail": "Scope {scope_id} not found"}` — GET and PUT only                                                  |
| `422` | Unprocessable Entity  | FastAPI validation error — POST and PUT only                                                                   |
| `500` | Internal Server Error | `{"detail": "PowerShell command failed", "ps_error": "..."}`                                                   |
| `503` | Service Unavailable   | `{"detail": "...", "reason": "..."}` or `{"status": "error", "detail": "...", "reason": "..."}` for `/healthz` |

## Reconciliation Contract

Crossplane reconciles every ~60 seconds: GET current state → compare to desired PUT body → issue PUT on any diff.

Rules that must hold to prevent infinite reconciliation loops:

- GET response must be byte-identical to the desired PUT body when no change is intended.
- No hidden defaults or transformations inside the API.
- Exclusions in values files **must** be in ascending IP numerical order — the API always returns them sorted.
- DNS server order must match exactly — the API preserves insertion order, never sorts.

**Removing failover with layered values files:** use `failover: null` — not `failover: {}`.
Helm deep-merges `{}` with the parent map, leaving failover intact. Only `null` removes it.

## CI Validation

Two scripts validate `dhcp_values` before anything reaches Crossplane:

```bash
# Validate one cluster directly
python scripts/validate_dhcp_values.py sites/site-a/mce/mce-1/hosted-cluster/cluster-1.yaml

# Auto-detect changed cluster files from git and validate each with its full merge chain
python scripts/validate_changed_clusters.py

# Validate all clusters regardless of what changed
python scripts/validate_changed_clusters.py --all
```

Install CI dependencies (pydantic + PyYAML only — no FastAPI stack needed):

```bash
pip install -r scripts/requirements.txt
```

GitLab CI job:

```yaml
validate-dhcp-values:
  stage: validate
  image: python:3.12-slim
  before_script:
    - pip install --quiet -r scripts/requirements.txt
  script:
    - python scripts/validate_changed_clusters.py
  rules:
    - changes:
        - sites/**/*.yaml
```

`validate_changed_clusters.py` is smart about scope:

- `sites/{site}/config.yaml` changed → validates all clusters in that site
- `sites/{site}/mce/{mce}/config.yaml` changed → validates all clusters in that MCE
- `sites/{site}/mce/{mce}/hosted-cluster/{cluster}.yaml` changed → validates just that cluster

## Security and Safety

- Bearer token auth via `DHCP_API_TOKEN` — optional; disabled when unset
- Runtime environment guard rejects all scope operations on non-Windows / non-DHCP hosts
- `-ErrorAction Stop` on every PowerShell command
- Shared secrets are never logged
- PowerShell stderr is sanitized before returning to clients
- Structured JSON logs include `scope_id`, `operation`, `result`, `duration`

## Testing

```bash
pytest
```

Test coverage includes:

- Endpoint contracts and HTTP status codes
- Pydantic schema validation (IPs, subnet consistency, range ordering, failover mode enforcement)
- PowerShell output parsing and normalization
- Diff-based update semantics (only changed sections trigger cmdlets)
- Runtime environment guard behavior
- GET/PUT parity contract — the main guard against Crossplane reconciliation loops

## Operational Notes

- This service must run on a Windows host with DHCP cmdlets available.
- Linux / macOS / WSL requests to scope endpoints return a structured `503` with a `reason` field.
- `/healthz` is always safe to call regardless of OS.
- Scope deletion is fail-safe: failover is detached before scope removal to prevent orphaned relationships.
- If failover detach fails, the delete is retried on the next Crossplane reconciliation cycle.
