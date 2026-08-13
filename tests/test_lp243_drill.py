"""LP-243 drill — a deliberately failing test. Branch drill/lp-243 only. NEVER MERGE.

Branch protection now requires the two CI checks to pass before `main` will accept a
merge, and `main` is what the deploy workflow triggers on. That is the mechanism. This
file is the demonstration: a red check must make the merge button refuse.

A configuration nobody has watched refuse something is a configuration you hope works.
"""


def test_this_fails_on_purpose_to_prove_the_gate_refuses() -> None:
    assert False, (
        "LP-243 drill. If you are reading this in a real run, the branch drill/lp-243 "
        "escaped — delete it."
    )
