# fabric-kg 0.2.4 Installed CLI Smoke Test

Run from the candidate checkout:

```bash
./scripts/smoke-test-0.2.4.sh
```

The script creates a clean `git archive`, builds wheel and sdist outside the
repository, installs the wheel non-editably into an external Python 3.12 virtual
environment, unsets `PYTHONPATH`, verifies package origin/version/36 top-level
commands, and exercises L7 dry-run and live rejection using local generic
observations. It does not import project modules from the repository.

Install the base wheel only for 0.2.4 acceptance. GitHub Copilot runs the CLI as
a local subprocess. `[agent]` is optional for Foundry agent management and
`[app]` is optional for future hosted integration; neither is a release gate.

## Results

- Candidate SHA:
- Python:
- Installed package origin:
- Wheel SHA-256:
- Sdist SHA-256:
- Plan SHA-256:
- Result:

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
