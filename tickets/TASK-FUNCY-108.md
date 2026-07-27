# TASK-FUNCY-108

## Goal
rcurry works on methods of built-in types, the same way it works on ordinary
Python functions. Today rcurry(str.endswith) raises while trying to work out how
many arguments the method takes, so a whole class of callables cannot be curried
at all.

## Validation
```
pytest -q
```

## Acceptance
- [ ] A built-in type's method can be curried and then called `check: python -c "import sys; from funcy import rcurry; sys.exit(0 if rcurry(str.endswith)('.com')('example.com') is True else 1)"`
- [ ] Currying an ordinary Python function keeps working exactly as it does now
- [ ] A callable whose arguments genuinely cannot be determined still fails clearly, rather than being guessed at and failing later somewhere confusing
- [ ] The existing tests still pass

## Constraints
- Do not change any file other than funcy/_inspect.py and funcy/funcs.py
- Do not delete or weaken an existing test to make this pass

## Files
- funcy/_inspect.py
- funcy/funcs.py
