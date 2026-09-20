# Drawbridge validation harness

## Entry and purpose

From the repository root, on Python 3.12+:

```sh
uv sync --frozen --extra dev
.venv/bin/python scripts/verify.py
```

The harness returns exit code 0 only after all gates pass. `--output /path/to/new-directory` chooses a fresh evidence directory; the default is `var/verification/<UTC timestamp>/`. The output directory must not already exist. The command may run from any working directory because paths are resolved from the script location.

This is the repeatable, local counterpart to the 2026-09-20 t4 verification in `VERIFICATION_RECORD.md`. It uses `config.example.yaml` as a template, creates a temporary Git repository from `examples/demo-app`, and redirects state, logs, releases, templates, and data into that temporary directory. It does not operate on an installed application or the repository's existing `var/` state. The temporary fixture is removed after the run; the logs and result summary remain in the evidence directory.

## Gates and evidence

| Gate | What must pass | Evidence |
| --- | --- | --- |
| Ruff | `ruff check src tests scripts` | `ruff.log` |
| Formatting | `ruff format --check src tests scripts` | `format.log` |
| mypy | Strict check of `src/drawbridge` | `mypy.log` |
| pytest | Entire test suite, including Gateway anonymous `401` and authenticated `ops_catalog` MCP call | `pytest.log` |
| Compilation | Python sources compile | `compileall.log` |
| Host self-check | All blocking checks pass for the isolated simulation config | `self_check.log`, `report.json` |
| Simulation | Register → plan → apply → run job → status; final job succeeded, health passed, source SHA matches, release artifacts exist, and SQLite contains plan/job/release/event rows | `simulation.log`, `report.json` |

`report.json` contains the overall result, time, host/Python/git revision, self-check details, and the simulation IDs and row counts. Each step log includes its command, exit code, stdout, and stderr. A failed gate stops subsequent gates, writes `result: failed` with the error, and returns a nonzero exit code. Do not treat the existence of a report as a pass; read `result`.

## Agent workflow

1. Read `TECHNICAL_DESIGN.md` and `MVP_IMPLEMENTATION_SPEC.md` for behavior and security invariants relevant to the change. `src/drawbridge/gateway.py` owns ingress/MCP; `service.py` owns operations and deployment flow; `storage.py` owns persisted state; `config.py` owns configuration validation. The corresponding tests are in `tests/`.
2. Add or adjust focused tests for changed behavior. Run the focused test while developing, then run the full harness before reporting completion.
3. Inspect `report.json` and failed logs. Record the actual run path, environment, counts, and scope in the handoff. Preserve `VERIFICATION_RECORD.md` as a dated record; append clearly dated results rather than replacing prior evidence.
4. When a change affects real deployment, authentication, outbound HTTP, or process execution, also review `docs/OPERATIONS.md` and the design constraints. A passing simulation harness does not prove the live Docker/BuildKit path.

## Scope and server validation

The harness deliberately avoids network fetches, real container builds, Docker Compose updates, and changes to server state. The Gateway test uses an in-process ASGI client; it verifies policy and MCP tool dispatch, not a deployed network listener. `self-check` treats rootless BuildKit as nonblocking for the simulation profile. For real deployment validation, use the server install and acceptance procedure in `docs/OPERATIONS.md` with an administrator-approved config, rootless BuildKit, and explicit runtime checks. Never infer production readiness from `report.json` alone.
