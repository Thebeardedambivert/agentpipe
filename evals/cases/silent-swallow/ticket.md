# TASK-EVAL-SWALLOW

## Goal
record_metric(store, row) writes one metric row and must never fail the caller,
because a meter that can take down the thing it measures is worse than no meter.

## Validation
```
python -c "import sys; sys.exit(0)"
```

## Acceptance
- [ ] a store failure never propagates to the caller
- [ ] a swallowed failure is reported somewhere a human will see it

## Files
- metrics.py
