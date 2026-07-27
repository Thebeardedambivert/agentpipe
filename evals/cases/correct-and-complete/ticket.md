# TASK-EVAL-CHUNK

## Goal
chunk(items, size) splits a list into consecutive pieces of at most size items,
without losing or duplicating anything.

## Validation
```
python -c "import sys; sys.exit(0)"
```

## Acceptance
- [ ] a size of zero or less raises rather than looping forever
- [ ] every piece except possibly the last holds exactly size items

## Files
- chunk.py
