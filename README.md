# brain_agent

`brain_agent` is a local WorldQuant BRAIN alpha-mining orchestrator. It coordinates idea generation, inspection, simulation, enhancement, reporting, memory, and submission-readiness checks while keeping credentials outside the repository.

## Repository Layout

The Python package files live at the repository root. If this repository is cloned as `brain_agent`, run it from the parent directory:

```bash
python3 -m brain_agent --help
```

From inside the repository directory, run it with the parent directory on `PYTHONPATH`:

```bash
PYTHONPATH=.. python3 -m brain_agent --help
```

## Credentials

Do not commit credentials to this repository. The agent reads credentials from environment variables or a local secret file.

Environment variables:

```bash
export BRAIN_EMAIL=<brain-email>
export BRAIN_PASSWORD=<brain-password>
export MOONSHOT_API_KEY=<moonshot-api-key>
```

Optional local secret file path:

```text
~/secrets/worldquant-brain.json
```

Expected shape:

```json
{
  "brain": {
    "email": "your_email",
    "password": "your_password"
  },
  "llm": {
    "moonshot_api_key": "your-api-key"
  }
}
```

The repository `.gitignore` uses a default-deny policy so runtime data, documents, caches, and secret-like files stay local.

## Basic Commands

```bash
PYTHONPATH=.. python3 -m brain_agent doctor --check-llm
```

```bash
PYTHONPATH=.. python3 -m brain_agent run \
  --dataset fundamental31 \
  --region EUR \
  --delay 1 \
  --universe TOP2500 \
  --data-type Matrix \
  --decay 10 \
  --truncation 0.08 \
  --neutralization SLOW_AND_FAST \
  --max-trade False \
  --target-ready 1 \
  --max-iterations 1 \
  --max-sim-alphas 1
```

```bash
PYTHONPATH=.. python3 -m brain_agent report --run-id <run_id>
```
