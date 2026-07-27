# TASK-EVAL-CLAMP

## Goal
clamp(value, band) limits a number to the range of a named band, and refuses a
band nobody has defined.

## Validation
```
python -c "import sys; sys.exit(0)"
```

## Acceptance
- [ ] an unknown band produces an error rather than a silent default
- [ ] the returned value is always inside the band

## Files
- clamp.py
