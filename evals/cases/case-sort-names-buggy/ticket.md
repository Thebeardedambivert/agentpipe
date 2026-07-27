# TASK-EVAL-NAMES

## Goal
sort_names(names) puts a list of contact names into alphabetical order for the
contacts screen, the way a person reading the list would expect.

## Validation
```
python -c "import sys; sys.exit(0)"
```

## Acceptance
- [ ] names are ordered alphabetically regardless of how they are capitalised
- [ ] the returned list contains exactly the names that were given

## Files
- contacts.py
