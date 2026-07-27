# TASK-EVAL-SLUG

## Goal
slugify(title) turns an article title into a slug that is safe to put in a URL
without escaping.

## Validation
```
python -c "import sys; sys.exit(0)"
```

## Acceptance
- [ ] the slug is lowercase with spaces replaced by hyphens
- [ ] punctuation is removed, so the slug needs no URL escaping

## Files
- slugify.py
