"""Eval harness tests.

The judge already has tests. These test the thing that grades the judge, which is
the part nobody else is watching. A scorer that miscounts would report a clean
matrix over a broken sensor and become the sixth entry in STATE.md's list of
failures that reported success while quietly not doing the thing, except this one
would be a measurement lying about a measurement.

So the centre of gravity here is deliberately odd: most of these tests point a
*known-wrong* judge at a *known-good* dataset and insist the report says so. A
scorer is only trustworthy if it can be made to fail on demand.

A fake model returning a canned verdict, so these are free and deterministic and
never call OpenAI. Self-contained fake (the CI lesson: a bare `pytest` has no
repo root on sys.path).
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from agentpipe.evals import (
    EvalError,
    Label,
    load_case,
    load_cases,
    materialise,
    report_dataset,
    report_evals,
    run_evals,
)
from agentpipe.evalstore import InMemoryEvalStore, record_eval_scores
from agentpipe.judge import CriterionOutcome, JudgeVerdict
from agentpipe.telemetry import InMemoryCallStore, MeteredClient, PriceMap

PRICES = PriceMap({"fake": {"input": 1.0, "cached_input": 0.1, "output": 10.0}})

TICKET = """# TASK-EVAL-FIXTURE

## Goal
truncate(text, length) shortens a string to at most length characters, safely.

## Validation
```
python -c "import sys; sys.exit(0)"
```

## Acceptance
- [ ] truncate rejects a negative length with a clear error
- [ ] the result never exceeds length characters

## Files
- truncate.py
"""

CODE = "def truncate(text, length):\n    return text[:length]\n"

# Criterion 0 is not met by CODE, criterion 1 is. So the honest verdict is BLOCK,
# and a correct judge disagrees with exactly nothing.
LABELS = [
    {"criterion": 0,
     "text": "truncate rejects a negative length with a clear error",
     "expect": "not_satisfied",
     "why": "no guard; a negative length slices from the end"},
    {"criterion": 1,
     "text": "the result never exceeds length characters",
     "expect": "satisfied",
     "why": "the slice is bounded by length"},
]


class FakeOpenAI:
    """Answers every criterion the same way, and counts calls.

    A judge whose behaviour is known exactly, so any number the scorer produces
    can be checked against arithmetic rather than against a second opinion.
    """

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.calls = 0
        self.seen: list[dict] = []

    @property
    def chat(self):
        return SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        self.seen.append(kwargs)
        body = [
            {"criterion": i, "outcome": self.outcome, "reason": f"canned {i}"}
            for i in range(2)
        ]
        reply = "--- verdict\n" + json.dumps(body) + "\n--- end"
        return SimpleNamespace(
            model="fake",
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=reply), finish_reason="stop",
            )],
            usage=SimpleNamespace(
                prompt_tokens=1000, completion_tokens=60,
                prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            ),
        )


class PerfectOpenAI(FakeOpenAI):
    """Answers each criterion exactly as the labels say. The zero-disagreement case."""

    def _create(self, **kwargs):
        self.calls += 1
        body = [
            {"criterion": lb["criterion"], "outcome": lb["expect"],
             "reason": f"agrees with label {lb['criterion']}"}
            for lb in LABELS
        ]
        reply = "--- verdict\n" + json.dumps(body) + "\n--- end"
        return SimpleNamespace(
            model="fake",
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=reply), finish_reason="stop",
            )],
            usage=SimpleNamespace(
                prompt_tokens=1000, completion_tokens=60,
                prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            ),
        )


class BrokenOpenAI(FakeOpenAI):
    """Replies with prose. The judge that cannot be parsed at all."""

    def _create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            model="fake",
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="Looks good to me!"),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(
                prompt_tokens=1000, completion_tokens=8,
                prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            ),
        )


def client_for(fake) -> MeteredClient:
    return MeteredClient(store=InMemoryCallStore(), prices=PRICES,
                         client=fake, run_id="eval-run")  # type: ignore[arg-type]


def write_case(root, name="fixture", *, labels=None, ticket=TICKET, code=CODE,
               provenance="constructed", source=None, files=("truncate.py",)):
    """Build one case directory on disk. Returns its path."""
    d = root / name
    (d / "code").mkdir(parents=True)
    (d / "ticket.md").write_text(ticket, encoding="utf-8", newline="\n")
    for f in files:
        (d / "code" / f).write_text(code, encoding="utf-8", newline="\n")
    meta = {
        "provenance": provenance,
        "files": list(files),
        "labels": LABELS if labels is None else labels,
        "note": "a fixture",
    }
    if source:
        meta["source"] = source
    (d / "case.json").write_text(json.dumps(meta), encoding="utf-8", newline="\n")
    return d


@pytest.fixture
def case_dir(tmp_path):
    write_case(tmp_path)
    return tmp_path


# --- the scorer, pointed at judges whose answers are known ------------------
#
# Each of these asks: when the judge is wrong in a specific way, does the report
# say so? A scorer that cannot be made to fail is not measuring anything.

def test_a_judge_that_always_passes_is_counted_as_a_false_pass(case_dir):
    """The dangerous quadrant. The gate would wave the unmet criterion through."""
    cases = load_cases(case_dir)
    fake = FakeOpenAI("satisfied")
    run = run_evals(cases, client_for(fake), "fake")

    false_passes = [s for s in run.scores if s.is_false_pass]
    assert len(false_passes) == 1
    assert false_passes[0].criterion_index == 0
    # And it must not also be counted as agreement anywhere.
    assert sum(1 for s in run.scores if s.agrees) == 1  # only criterion 1
    assert not any(s.is_false_block for s in run.scores)

    # The verdict is wrong too: PASS where the labels say BLOCK.
    assert run.cases[0].expected_verdict is JudgeVerdict.BLOCK
    assert run.cases[0].actual_verdict is JudgeVerdict.PASS
    assert not run.cases[0].verdict_agrees


def test_a_judge_that_always_blocks_is_counted_as_a_false_block(case_dir):
    """Correct code sent back to the builder. Only expensive since Stage 2."""
    cases = load_cases(case_dir)
    run = run_evals(cases, client_for(FakeOpenAI("not_satisfied")), "fake")

    false_blocks = [s for s in run.scores if s.is_false_block]
    assert len(false_blocks) == 1
    assert false_blocks[0].criterion_index == 1
    assert not any(s.is_false_pass for s in run.scores)


def test_blocking_everything_does_not_count_as_being_right(case_dir):
    """The degenerate strategy must not score well.

    A judge that blocks everything reaches the right VERDICT on this case, because
    the case's honest verdict is BLOCK. That is exactly the trap: counting verdicts
    alone rewards a sensor that is not reading anything. right_for_the_right_reason
    is what separates them, and it is the number that matters, because the builder
    is sent back to fix the criteria the judge named.
    """
    cases = load_cases(case_dir)
    run = run_evals(cases, client_for(FakeOpenAI("not_satisfied")), "fake")
    case = run.cases[0]

    assert case.verdict_agrees            # right answer
    assert not case.right_for_the_right_reason  # wrong reasoning
    assert any(s.is_false_block for s in case.criteria)


def test_uncertain_is_an_abstain_and_never_an_agreement(case_dir):
    """'I cannot tell' must not be scored as either a hit or a confident miss."""
    cases = load_cases(case_dir)
    run = run_evals(cases, client_for(FakeOpenAI("uncertain")), "fake")

    assert all(s.is_abstain for s in run.scores)
    assert not any(s.agrees for s in run.scores)
    assert not any(s.is_false_pass or s.is_false_block for s in run.scores)


def test_a_perfect_judge_disagrees_with_nothing(case_dir):
    """The control. If this fails, every other number here is noise."""
    cases = load_cases(case_dir)
    run = run_evals(cases, client_for(PerfectOpenAI("ignored")), "fake")

    assert all(s.agrees for s in run.scores)
    assert not any(
        s.is_false_pass or s.is_false_block or s.is_abstain for s in run.scores
    )
    assert run.cases[0].right_for_the_right_reason
    assert run.unusable == 0


def test_an_unusable_reply_is_counted_not_swallowed(case_dir):
    """A judge that cannot be parsed is a sensor failure, and a reportable one.

    In production this is precisely what makes the gate fail open: the code passes
    ungated with a note. If the eval quietly dropped these samples, a judge that is
    unusable half the time would report a flawless matrix over the half it managed.
    """
    cases = load_cases(case_dir)
    run = run_evals(cases, client_for(BrokenOpenAI("ignored")), "fake")

    assert run.unusable == 1
    assert run.scores == ()
    assert run.cases[0].error is not None
    assert run.cases[0].actual_verdict is None
    assert not run.cases[0].verdict_agrees
    assert "fail open" in report_evals(run)

    # And it is not free. The reply was unusable; the call was still billed. An
    # exception that drops the record makes an unusable judge look like a cheap
    # one, which is the wrong way round: it is the failure that spends money and
    # returns nothing.
    assert run.cases[0].cost_usd > 0
    assert run.billed_cost_usd > 0


def test_the_report_never_prints_a_percentage(case_dir):
    """Counts, not rates. At this size a percentage is a story, not a measurement."""
    cases = load_cases(case_dir)
    run = run_evals(cases, client_for(FakeOpenAI("satisfied")), "fake")
    text = report_evals(run)

    assert "%" not in text
    assert "false pass 1" in text


def test_real_and_constructed_are_never_merged(tmp_path):
    """The constructed half was written by whoever wrote the judge's prompt.

    Averaging it in with real cases is a number flattering itself, so the report
    has to carry both cuts separately.
    """
    write_case(tmp_path, "one", provenance="real", source="TASK-REAL-1")
    write_case(tmp_path, "two", provenance="constructed")
    cases = load_cases(tmp_path)
    run = run_evals(cases, client_for(FakeOpenAI("satisfied")), "fake")
    text = report_evals(run)

    assert "  real " in text
    assert "  constructed " in text
    assert len({s.provenance for s in run.scores}) == 2


# --- repeats: genuinely new calls, not cache replays ------------------------

def test_repeat_makes_distinct_paid_calls(case_dir):
    """Each sample runs at a distinct attempt_index, which is in the idempotency key.

    Without that, sample 1 would replay sample 0 and the harness would report
    perfect stability for every judge, having asked the question once. That is the
    cache measuring itself, which is worse than not measuring.
    """
    cases = load_cases(case_dir)
    fake = FakeOpenAI("satisfied")
    run = run_evals(cases, client_for(fake), "fake", repeat=3)

    assert fake.calls == 3            # three real calls, no replays
    assert len(run.cases) == 3
    assert sorted(c.sample for c in run.cases) == [0, 1, 2]
    assert len(run.scores) == 6
    assert sorted({s.sample for s in run.scores}) == [0, 1, 2]


def test_a_replayed_sample_is_not_counted_as_spend(case_dir):
    """Found on the first real --repeat run, 23 Jul 2026.

    Grading 8 cases x 5 reported $0.024944, but sample 0 of every case replayed
    from the run before it: only $0.019923 was actually billed. cost_usd on a
    replayed record carries the original price, which is right for "what does this
    dataset cost to grade" and wrong for "what did this invocation spend". The
    report now says both.

    The second half matters more than the money: a replayed sample is not an
    independent draw. Five replays of one answer are one answer, so counting them
    toward stability would report perfect consistency for a judge that was asked
    once.
    """
    cases = load_cases(case_dir)
    client = client_for(FakeOpenAI("satisfied"))

    first = run_evals(cases, client, "fake")
    assert first.replayed == 0
    assert first.billed_cost_usd == first.total_cost_usd > 0

    # Same client, same store: sample 0 is byte-identical work at the same
    # attempt_index, so the seam hands back the recorded answer.
    second = run_evals(cases, client, "fake", repeat=2)
    assert second.replayed == 1
    assert second.billed_cost_usd < second.total_cost_usd
    assert second.billed_cost_usd == second.total_cost_usd - first.total_cost_usd

    text = report_evals(second)
    assert "billed" in text
    assert "replayed from cache" in text


def test_repeat_below_one_is_refused(case_dir):
    cases = load_cases(case_dir)
    with pytest.raises(EvalError):
        run_evals(cases, client_for(FakeOpenAI("satisfied")), "fake", repeat=0)


def test_eval_calls_are_tagged_so_they_are_not_mistaken_for_production(case_dir):
    """Eval spend is real and lands in model_calls. It must be unmistakable there.

    It also keeps the two off one idempotency key: judging a stored case is
    different work from judging the run that produced it, and a collision would
    hand one of them the other's answer.
    """
    cases = load_cases(case_dir)
    store = InMemoryCallStore()
    client = MeteredClient(store=store, prices=PRICES,
                           client=FakeOpenAI("satisfied"), run_id="eval-run")  # type: ignore[arg-type]
    run_evals(cases, client, "fake")

    refs = {r.task_ref for r in store.records.values()}
    assert refs == {"EVAL/fixture"}
    assert all(r.role == "judge" and r.attempt_kind == "eval"
               for r in store.records.values())


# --- the dataset refuses to be wrong ---------------------------------------

def test_a_label_that_no_longer_matches_its_criterion_is_refused(tmp_path):
    """The drift guard, and the reason labels carry the criterion text at all.

    Reorder a ticket's acceptance bullets and every label silently repoints at the
    wrong criterion. Nothing errors, the eval runs, and it reports a confident
    wrong number: the exact shape of all five bugs in STATE.md. This is the check
    that turns that silent failure into a loud one, for free, before any spend.
    """
    stale = [dict(LABELS[0]), dict(LABELS[1])]
    stale[0]["text"] = "truncate handles unicode correctly"  # ticket says otherwise
    d = write_case(tmp_path, "drifted", labels=stale)

    with pytest.raises(EvalError, match="The ticket moved and the labels did not"):
        load_case(d)


def test_labels_must_cover_every_criterion_exactly_once(tmp_path):
    """A partially labelled case is not a case.

    The same completeness rule parse_verdict enforces on the judge. Without it the
    unlabelled criteria are graded against nothing and vanish from the counts, so
    the denominator quietly shrinks and accuracy quietly improves.
    """
    d = write_case(tmp_path, "partial", labels=[LABELS[0]])
    with pytest.raises(EvalError, match="exactly once"):
        load_case(d)

    dup = write_case(tmp_path, "duplicated", labels=[LABELS[0], dict(LABELS[0])])
    with pytest.raises(EvalError, match="exactly once"):
        load_case(dup)


def test_uncertain_is_not_a_label(tmp_path):
    """A labeller who is uncertain has not finished making the case."""
    fuzzy = [dict(LABELS[0]), dict(LABELS[1])]
    fuzzy[0]["expect"] = "uncertain"
    d = write_case(tmp_path, "fuzzy", labels=fuzzy)

    with pytest.raises(EvalError, match="the judge's answer, not yours"):
        load_case(d)


def test_a_label_without_a_why_is_refused(tmp_path):
    """The why is how you tell, later, whether the judge or the label was wrong."""
    silent = [dict(LABELS[0]), dict(LABELS[1])]
    silent[0]["why"] = "  "
    d = write_case(tmp_path, "unexplained", labels=silent)

    with pytest.raises(EvalError, match="which of the two is"):
        load_case(d)


def test_a_ticket_with_no_judgeable_criteria_is_refused(tmp_path):
    """run_judge would return UNGUARDED and spend nothing.

    The case would contribute zero rows while looking like it took part, which is
    an unguarded gate wearing a dataset's clothes.
    """
    checked = TICKET.replace(
        "- [ ] truncate rejects a negative length with a clear error\n"
        "- [ ] the result never exceeds length characters",
        '- [ ] truncate exists `check: python -c "import truncate"`',
    )
    d = write_case(tmp_path, "unjudgeable", ticket=checked, labels=[LABELS[0]])

    with pytest.raises(EvalError, match="nothing for the judge to grade"):
        load_case(d)


def test_a_real_case_must_name_its_source(tmp_path):
    """'Real' with no run behind it is a claim nobody can check."""
    d = write_case(tmp_path, "unsourced", provenance="real")
    with pytest.raises(EvalError, match="no source is named"):
        load_case(d)


def test_a_case_naming_a_file_that_is_not_there_is_refused(tmp_path):
    d = write_case(tmp_path, "missing")
    meta = json.loads((d / "case.json").read_text(encoding="utf-8"))
    meta["files"] = ["nowhere.py"]
    (d / "case.json").write_text(json.dumps(meta), encoding="utf-8", newline="\n")

    with pytest.raises(EvalError, match="not in code/"):
        load_case(d)


def test_a_case_whose_ticket_the_pipeline_would_refuse_is_refused_here(tmp_path):
    """If the ticket contract rejects it, the case is not measuring the pipeline."""
    d = write_case(tmp_path, "vague", ticket="# TASK-X\n\n## Goal\nshort\n")
    with pytest.raises(EvalError, match="its ticket is not valid"):
        load_case(d)


def test_label_construction_rejects_an_unlabellable_outcome():
    with pytest.raises(EvalError):
        Label(0, "text", CriterionOutcome.UNCERTAIN, "why")


# --- the case reads through the real Repo -----------------------------------

def test_materialise_produces_a_real_git_repo(tmp_path):
    """Not a double.

    The judge reads through Repo.read() in production. A stand-in that behaves
    almost the same is how this project once had fifteen tests passing over a
    store that dropped content on the floor.
    """
    d = write_case(tmp_path, "one")
    case = load_case(d)
    dest = tmp_path / "materialised"
    repo = materialise(case, dest)

    assert (dest / ".git").exists()
    assert repo.read("truncate.py") == CODE
    # git actually tracks it: repo.files() shells out to git ls-files.
    assert repo.files() == ("truncate.py",)
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=dest, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert tracked == ["truncate.py"]


# --- recording --------------------------------------------------------------

def test_one_row_is_recorded_per_criterion_per_sample(case_dir):
    cases = load_cases(case_dir)
    run = run_evals(cases, client_for(FakeOpenAI("satisfied")), "fake", repeat=2)
    store = InMemoryEvalStore()
    record_eval_scores(store, "run-1", run.scores, run.rules)

    rows = store.for_run("run-1")
    assert len(rows) == 4  # 2 criteria x 2 samples
    assert {r.expected for r in rows} == {"not_satisfied", "satisfied"}
    assert {r.actual for r in rows} == {"satisfied"}
    assert {r.sample for r in rows} == {0, 1}
    assert all(r.rules_hash == run.rules for r in rows)
    assert all(r.call_key for r in rows)


def test_recording_never_fails_the_run(case_dir, capsys):
    """The audit must never take down the thing it measures. Doubly so here.

    This table is evidence about evidence: if a broken store could abort the eval,
    a database hiccup would look like a judge problem.
    """
    class Exploding(InMemoryEvalStore):
        def record(self, row):
            raise RuntimeError("database is on fire")

    cases = load_cases(case_dir)
    run = run_evals(cases, client_for(FakeOpenAI("satisfied")), "fake")
    record_eval_scores(Exploding(), "run-1", run.scores, run.rules)  # must not raise

    assert "WARN" in capsys.readouterr().out


# --- the shipped dataset ----------------------------------------------------

def test_the_shipped_dataset_loads_and_is_labelled_consistently():
    """The dataset in the repo is itself under test.

    Every guard above fires on a fixture; this fires on the real thing. It is what
    catches a label edited out of step with its ticket in a future commit, on the
    push rather than on the next paid run.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "evals" / "cases"
    if not root.is_dir():  # pragma: no cover - the dataset ships with the repo
        pytest.skip("no eval dataset in this checkout")

    cases = load_cases(root)
    assert len(cases) >= 8
    assert any(c.provenance == "real" for c in cases)
    assert any(c.provenance == "constructed" for c in cases)

    # Neither degenerate strategy may win: a judge that blocks everything and one
    # that passes everything must both be wrong on a meaningful share of cases. A
    # dataset made only of broken code silently rewards a judge that never reads.
    blocks = sum(1 for c in cases if c.expected_verdict is JudgeVerdict.BLOCK)
    passes = len(cases) - blocks
    assert blocks >= 2 and passes >= 2

    # Exercise the --dry-run path, but on a slice, NOT on the whole dataset.
    #
    # report_dataset materialises every case it is given, and materialising is a
    # real `git init` plus `git add` per case. This line originally ran over all
    # of them with the comment "free". It was not free: it was the most expensive
    # line in the file, and its cost grew every time a case was added. At eight
    # cases the file ran in 8 seconds; at twenty it went past three minutes on a
    # Windows box where Defender scans each new repo.
    #
    # The shape of the mistake matters more than the seconds. A test whose cost
    # scales with the dataset taxes the exact behaviour this dataset needs, which
    # is people adding cases to it. Two cases prove the function works; twenty
    # prove it twenty times and charge for the privilege.
    assert "cases" in report_dataset(cases[:2])
