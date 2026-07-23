# TASK-EVAL-ACCOUNT

## Goal
same_account(a, b) decides whether two email addresses a user has typed refer to
the same account, so sign-in does not create duplicates.

## Validation
```
python -c "import sys; sys.exit(0)"
```

## Acceptance
- [ ] addresses differing only in capitalisation are treated as the same account
- [ ] genuinely different addresses are treated as different accounts

## Files
- accounts.py
