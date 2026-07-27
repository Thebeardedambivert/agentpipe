# TASK-EVAL-TOTAL

## Goal
totals_match(prices, expected) reports whether a basket of prices adds up to the
expected amount, for the reconciliation step at checkout.

## Validation
```
python -c "import sys; sys.exit(0)"
```

## Acceptance
- [ ] amounts that are mathematically equal are reported as matching
- [ ] amounts that genuinely differ are reported as not matching

## Files
- totals.py
