# acme-service

The monitored production service for the incident-triage voice agent demo.

This repo stands in for Acme's customer-facing platform — `payments-api`,
`orders-api`, `checkout-api`, and the downstream `tax-service` — and is the
codebase the triage agent investigates and (on approval) patches.

The agent's tools read this repo directly:

- `get_deploy_history(service)` reads `git log` here, so deploy ids such as
  `abc123` and `def456` are **real commits** you can grep for.
- `read_repo_file(path)` serves source from `app/`.
- `propose_code_fix` / `apply_code_fix` stage and apply patches against these
  files.

## Layout

```
app/
  db.py           # payments-api DB engine + pool (incident #1)
  batch_jobs.py   # nightly orders-db reconciliation (incident #2)
  tax_service.py  # checkout-api → tax-service client (incident #4)
```

Each file carries a planted bug that one of the demo incidents diagnoses. The
canonical ground truth (root cause, proof, staged patch) lives in
`server/mock_backend.py` and `docs/cekura-eval-plan.md`.
