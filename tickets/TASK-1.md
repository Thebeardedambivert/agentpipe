# TASK-1

## Goal
The prices.example.json file exists at the repo root, so that the setup steps in
the README can be followed without hitting a missing file.

## Validation
```
pytest -q
```

## Acceptance
- [ ] prices.example.json exists at the repo root `check: python -c "import os, sys; sys.exit(0 if os.path.exists('prices.example.json') else 1)"`
- [ ] It contains null price values, not real numbers `check: python -c "import json, sys; d = json.load(open('prices.example.json')); vals = [v for m in d.values() if isinstance(m, dict) for v in m.values()]; sys.exit(0 if vals and all(x is None for x in vals) else 1)"`
- [ ] It explains that the reader must fill it in themselves
- [ ] No existing files are changed

## Constraints
- Do not look up or invent real model prices
- Do not modify telemetry.py

## Files
- prices.example.json
- README.md
