# fabric-kg 0.2.4 Installed CLI Smoke Test

Run from the candidate checkout:

```bash
./scripts/smoke-test-0.2.4.sh
```

The script creates a clean `git archive`, builds wheel and sdist outside the
repository, installs the wheel non-editably into an external Python 3.12 virtual
environment, unsets `PYTHONPATH`, verifies package origin/version/38 top-level
commands, and exercises L7 dry-run and live rejection using local generic
observations. It does not import project modules from the repository.

Install the base wheel only for 0.2.4 acceptance. GitHub Copilot runs the CLI as
a local subprocess.

## Results

- Candidate SHA:
- Python:
- Installed package origin:
- Wheel SHA-256:
- Sdist SHA-256:
- Plan SHA-256:
- Result:

### Run of 2026-08-30

- Candidate SHA: `4e33ef6`
- Python: 3.12.13
- Installed package origin: external venv `site-packages/fabric_kg_builder/__init__.py`
- Wheel SHA-256: `f3927b305da36eee274461b365fa8b4d52c9a5f6ca6f8f1b740813b6ae182a70`
- Sdist SHA-256: `39d28adc6c946989df6dc856627ba9f9722ce1fdf7419ba135ddeeb4d3370c5a`
- Plan SHA-256: `55201c7116b231ffc04f90b374907d59179217ce188775e60523a81f8cf222e5`
- Result: external base-wheel smoke passed; 36 top-level commands; dry-run
  reported `mutations=0` and `l6_hosting=generated-local-deferred`.

The subsequent authorized one-shot live run failed closed during preflight with
an Azure AI Search `HTTP 403` authorization blocker and performed zero
mutations. See `RELEASE-0.2.4-PROOF.md` for the exact blocker and remediation.

Live deployment is intentionally excluded from implementation-session smoke
testing. Attach only sanitized plan and receipt evidence after an authorized
operator supplies ignored external configuration.

The authorized parent workflow may run a one-shot live command:

```bash
fabric-kg app deploy-l7 --live \
  --config /external/l7-0.2.4.json \
  --plan /external/l7-plan.json \
  --out /external/l7-receipt.json \
  --log /external/l7-events.jsonl
```

The command completes read-only preflight and collision checks before its first
mutation. If it fails, use the sanitized causal stage and rollback receipt for
RCA and file a GitHub issue for a reproducible product defect.
