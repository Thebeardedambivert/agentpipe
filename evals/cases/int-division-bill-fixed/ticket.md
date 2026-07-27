# TASK-EVAL-SPLIT

## Goal
split_bill(total_pence, people) divides a bill between people and returns what
each one owes, in whole pence, for the group payments screen.

## Validation
```
python -c "import sys; sys.exit(0)"
```

## Acceptance
- [ ] the amounts always add up to exactly the total
- [ ] no two people are asked for amounts differing by more than one penny

## Files
- split.py
