# Drawbridge agent entry point

Read [docs/HARNESS.md](docs/HARNESS.md) before changing or verifying this repository. It is the canonical harness guide for Codex and other coding agents. The executable entry point is `scripts/verify.py`:

```sh
uv sync --frozen --extra dev
.venv/bin/python scripts/verify.py
```

The command prints the path to an ignored `var/verification/<UTC timestamp>/report.json` and exits nonzero on failure. Inspect that JSON and the adjacent step logs. For the prior t4 validation and its limits, read [VERIFICATION_RECORD.md](VERIFICATION_RECORD.md); it is historical evidence, not a live readiness flag.

The harness exercises the simulation profile in an isolated temporary directory. Real Docker/BuildKit deployment and production readiness require the separate server procedure in [docs/OPERATIONS.md](docs/OPERATIONS.md).
