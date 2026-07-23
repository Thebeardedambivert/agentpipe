# TASK-EVAL-PORT

## Goal
parse_port(raw) turns a string from configuration into a valid TCP port number,
refusing anything that is not one.

## Validation
```
python -c "import sys; sys.exit(0)"
```

## Acceptance
- [ ] a port outside 1..65535 is rejected rather than replaced with a default
- [ ] a valid port is returned unchanged

## Files
- ports.py
