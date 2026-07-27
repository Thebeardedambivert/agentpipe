# TASK-EVAL-RANK

## Goal
rank(players) orders a list of (name, score) pairs for a leaderboard, highest
score first, without disturbing players who are level with each other.

## Validation
```
python -c "import sys; sys.exit(0)"
```

## Acceptance
- [ ] players are ordered from the highest score to the lowest
- [ ] players with equal scores keep the order they were given in

## Files
- leaderboard.py
