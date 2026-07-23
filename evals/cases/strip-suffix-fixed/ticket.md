# TASK-EVAL-HOST

## Goal
normalise_host(host) removes a trailing ".com" from a hostname so stored
hostnames are consistent, and leaves everything else about the name alone.

## Validation
```
python -c "import sys; sys.exit(0)"
```

## Acceptance
- [ ] only a trailing ".com" is removed; characters elsewhere in the hostname are left alone
- [ ] a hostname that does not end in ".com" is returned unchanged

## Files
- hosts.py
