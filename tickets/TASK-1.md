# TASK-1

## Goal
The prices.example.json file exists at the repo root, so that the setup steps in
the README can be followed without hitting a missing file.

## Validation
```
pytest -q
```

## Acceptance
- [ ] prices.example.json exists at the repo root
- [ ] It contains null price values, not real numbers
- [ ] It explains that the reader must fill it in themselves
- [ ] No existing files are changed

## Constraints
- Do not look up or invent real model prices
- Do not modify telemetry.py

## Files
- prices.example.json
- README.md
