# TASK-TOOLZ-626

## Goal
tail(n, seq) returns the last n items for every kind of iterable, including when
n is zero. Today tail(0, [10, 20, 30]) gives back the whole list while
tail(0, iter([10, 20, 30])) correctly gives nothing, so the same call means two
different things depending on what was passed in.

## Validation
```
pytest -q toolz/tests/test_itertoolz.py
```

## Acceptance
- [ ] Asking for zero items returns no items, whether the input can be sliced or not `check: python -c "import sys; from toolz import tail; sys.exit(0 if tuple(tail(0, [10, 20, 30])) == () and tuple(tail(0, iter([10, 20, 30]))) == () else 1)"`
- [ ] The two kinds of input agree with each other for every n, not only for zero
- [ ] Asking for more items than the sequence holds still returns the whole sequence
- [ ] The existing itertoolz tests still pass

## Constraints
- Do not change any file other than toolz/itertoolz.py
- Do not delete or weaken an existing test to make this pass

## Files
- toolz/itertoolz.py
