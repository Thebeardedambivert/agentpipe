# TASK-FIX-ELLIPSIS

## Goal
truncate(text, length) shortens a string to at most length characters and makes
it obvious to a reader that something was cut off.

## Validation
```
python -c "import sys; sys.exit(0)"
```

## Acceptance
- [ ] the result never exceeds length characters
- [ ] a reader can tell that the text was shortened

## Files
- truncate.py
