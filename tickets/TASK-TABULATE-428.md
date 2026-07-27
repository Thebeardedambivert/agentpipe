# TASK-TABULATE-428

## Goal
Asking tabulate not to interpret cell values as numbers is respected even when a
column width limit is set. Today passing disable_numparse=True together with
maxcolwidths raises ValueError on a cell like '80,443', so the two options cannot
be used together at all.

## Validation
```
python -m pytest -q test/test_output.py
```

## Acceptance
- [ ] A table with disable_numparse=True and maxcolwidths set renders instead of raising, and the cell keeps its original text `check: python -c "import sys; from tabulate import tabulate; out = tabulate([['ports', 'str', 'a port list', '80,443']], ['name', 'type', 'desc', 'default'], tablefmt='grid', disable_numparse=True, maxcolwidths=40); sys.exit(0 if '80,443' in out else 1)"`
- [ ] Cells that look like numbers are still treated as numbers when disable_numparse is not asked for, including the alignment that follows from that
- [ ] Setting maxcolwidths still wraps long cells to the width given
- [ ] disable_numparse given as a list of column indexes keeps working the same way it does now
- [ ] The existing output tests still pass

## Constraints
- Do not change any file other than tabulate/__init__.py
- Do not delete or weaken an existing test to make this pass

## Files
- tabulate/__init__.py
