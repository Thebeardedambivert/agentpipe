# TASK-BOLTONS-301

## Goal
FunctionBuilder.from_func(f).get_func() returns a function that behaves like f.
Today the rebuilt function runs and returns None, because the original body is
never carried across, so every function rebuilt this way silently does nothing.

## Validation
```
pytest -q tests/test_funcutils.py
```

## Acceptance
- [ ] A function rebuilt through from_func().get_func() returns what the original function returns `check: python -c "import sys; from boltons.funcutils import FunctionBuilder as B; from boltons.strutils import ordinalize; sys.exit(0 if B.from_func(ordinalize).get_func()(1) == '1st' else 1)"`
- [ ] The reported case behaves: for a function `def foo(a, b=2): return a / b`, calling `FunctionBuilder.from_func(foo).get_func()(20)` gives 10.0, not None
- [ ] The existing funcutils tests still pass
- [ ] Functions rebuilt this way keep the name, signature and defaults they already kept before this change

## Constraints
- Do not change any file other than boltons/funcutils.py
- Do not delete or weaken an existing test to make this pass

## Files
- boltons/funcutils.py
