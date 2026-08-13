"""What the image pipeline handles, and what it does not (LP-202, DEL-6).

This lives in code rather than in a document because a limitations list in a document is
a limitations list as of the day someone wrote it. `scripts/robustness_eval.py --docs`
renders `docs/robustness.md` from here and a test fails if the committed file has drifted,
so the honest account and the code that has to be honest cannot come apart.

**Why this list is a deliverable and not an apology.** The failure mode this whole product
is arguing against is a tool that sounds certain about something it could not see. A
robustness section that lists only what works is the same failure in prose. Every entry
below names something a reviewer could have found on their own; finding it here first is
the point.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Limitation:
    """One honest boundary: what works, what does not, and how we know."""

    area: str
    handled: str
    not_handled: str
    why: str
    evidence: str
    runs_in_production: bool = True
    """False when the capability is built and tested but nothing calls it in the app.

    This distinction is the whole point of the document, and getting it wrong is the exact
    failure the document exists to prevent. `verify_endpoint` calls ingest and
    `quality.assess`, so the pre-gate is live. It never calls `preprocess.preprocess`,
    `assess_region` or `illegible_regions` — deskew, exposure lift, glare recovery and
    per-region readability run in tests and harnesses only. Listing those under "handled"
    described a system nobody had built, in a document whose premise is not overclaiming.

    The route belongs to another wave, so wiring it is a coordination problem rather than
    a code one; `WIRING` below is the exact change.
    """


LIMITATIONS: list[Limitation] = [
    Limitation(
        area="Perspective and angle",
        handled=(
            "Off-axis photographs up to 45° are rectified from the label's four corners "
            "when a boundary is visible, and in-plane rotation is measured and undone."
        ),
        not_handled=(
            "A label photographed edge to edge has no boundary, so nothing is rectified — "
            "the geometry is left alone and the extractor reads it as it arrived. "
            "Detection fires on 4 of 19 robustness conditions for exactly this reason."
        ),
        why=(
            "There is nothing to detect. Inferring corners that are not in the frame would "
            "mean guessing where the label ends, and a wrong guess is a crop."
        ),
        evidence="scripts/crop_before_send.py, tests/test_deskew.py",
        runs_in_production=False,
    ),
    Limitation(
        area="Curved surfaces",
        handled=(
            "A label wrapped around a bottle stays legible and is scored normally; the "
            "centre is sharper than the edges, which is what a cylinder does."
        ),
        not_handled=(
            "The curvature is not flattened. A four-point transform cannot undo it and "
            "the pass reports perspective_applied=False rather than claiming otherwise."
        ),
        why=(
            "Cylindrical distortion is not projective. Unwrapping it would need an assumed "
            "bottle radius — a parameter nobody measured, applied to every image, to fix a "
            "case the extractor already handles."
        ),
        evidence="tests/test_robustness.py::test_a_bottle_curve_is_not_claimed_to_be_flattened",
        runs_in_production=False,
    ),
    Limitation(
        area="Lighting",
        handled=(
            "Underexposed photographs are lifted with local contrast, including the "
            "realistic case of a bottle lit from one side."
        ),
        not_handled=(
            "Images that are already well exposed are never touched, even where a "
            "particular region is hard to read. And lifting a *dim* photograph does raise "
            "a buried warning's contrast relative to the rest of the label — measured at "
            "0.379 before and 0.521 after on TC-06's fixture — so a prominence judgement "
            "made on the processed pixels would see the violation as less severe than it "
            "is. The uploaded pixels travel with the result for exactly that reason."
        ),
        why=(
            "A warning printed pale grey on cream is a prominence violation (WARN-5, "
            "TC-06). Lifting contrast on every image would deliver that violation to the "
            "extractor looking perfectly legible and turn a real finding into a pass. A "
            "dim photograph has to be lifted or nothing on it can be read at all, so that "
            "case is handled by keeping the original rather than by refusing to help."
        ),
        evidence="tests/test_preprocess.py::test_preprocessing_leaves_a_buried_warning_buried",
        runs_in_production=False,
    ),
    Limitation(
        area="Glare",
        handled=(
            "Detail in the near-saturated shoulder around a highlight is recovered, and "
            "the blown-out core is published as a mask so whatever sits under it is "
            "reported Unreadable per field."
        ),
        not_handled=(
            "Nothing is reconstructed under a blown-out area. No inpainting, at all."
        ),
        why=(
            "A pixel at 255 carries no information about what was under it. Inpainting "
            "produces *confident* pixels, so the extractor would read clean text off a "
            "region the camera never saw and the verdict would carry a false pass with "
            "evidence attached."
        ),
        evidence="tests/test_preprocess.py::test_a_blown_pixel_is_never_filled_in",
        runs_in_production=False,
    ),
    Limitation(
        area="Blur",
        handled=(
            "Defocus and directional camera shake are both measured, as the worst of "
            "eight orientations after a noise-suppressing pre-smooth. Small print goes "
            "illegible before large print does, which is why readability is judged per "
            "region."
        ),
        not_handled=(
            "Nothing is deconvolved or sharpened, and a directional measure over-flags "
            "content that is genuinely one-directional — perfectly sharp ruled lines or a "
            "barcode score as blurred, because along their own axis there is no detail to "
            "find. The obvious fix, skipping directions with no coarse structure, was "
            "measured and rejected: it turns a detected 0° smear back into a pass."
        ),
        why=(
            "Sharpening invents edges. The failure being avoided is not rejecting a bad "
            "photo — it is an extractor confidently returning plausible field values from "
            "mush. Over-flagging a barcode costs a retake request; under-flagging a smear "
            "costs a false verdict, and those do not trade."
        ),
        evidence=(
            "tests/test_quality.py blur cases, fixtures tc14_shake_diagonal_30/45/60"
        ),
    ),
    Limitation(
        area="Wrong subject",
        handled="Nothing here. The pre-gate catches illegible images.",
        not_handled=(
            "A photograph of a cat is sharp, well exposed, correctly scored, and passed "
            "straight through to the model."
        ),
        why=(
            "Quality scoring answers 'can this be read', not 'is this a label'. TC-15 is "
            "the model's job and always was."
        ),
        evidence="tests/test_quality.py::test_a_sharp_non_label_image_passes_the_pre_gate",
    ),
    Limitation(
        area="Thresholds",
        handled=(
            "Every gate value is a named constant, swept by a script that reports false "
            "passes and false flags at each level, with the chosen values and their "
            "margins recorded."
        ),
        not_handled=(
            "They are calibrated against generated labels, not photographs. Several "
            "'clean bands' are wide because the set is coarse, which is an absence of "
            "evidence rather than evidence of safety."
        ),
        why=(
            "Real optics differ from simulated ones, and this is the most likely place "
            "for the pipeline to be wrong. Tier B has since been captured — three real "
            "bottles, 21 scored rows, 15 correct — and it found exactly this class of "
            "defect: see docs/accuracy.md."
        ),
        evidence="scripts/calibrate_quality.py, api/rules/thresholds.py",
    ),
    Limitation(
        area="The fixtures themselves",
        handled=(
            "Nineteen degradations, committed as files with digests, each declaring what "
            "the pipeline owes it, regenerable byte for byte."
        ),
        not_handled=(
            "They simulate optics, not physics. A Gaussian is not lens blur, an overlay "
            "is not a specular highlight on curved glass, and added Gaussian noise is not "
            "a real sensor at high ISO. The false-pass column is also computed over only "
            "9 of the 19 conditions, because a condition whose obligation is `readable` "
            "has nothing illegible to miss."
        ),
        why=(
            "Reproducibility, which regression tests require and photographs cannot give. "
            "The gap between this set and real bottles is Tier B's job, and the difference "
            "between the two numbers is the honest answer to 'does this work'."
        ),
        evidence="fixtures/robustness/manifest.json",
    ),
    Limitation(
        area="Region readability",
        handled=(
            "Quality is scored per evidence region, so a warning under glare comes back "
            "Unreadable while the brand above it is verified."
        ),
        not_handled=(
            "Region boxes come from the extractor, so a field the extractor never located "
            "has no region to score. On the generated set the boxes are measured from the "
            "renderer's own layout."
        ),
        why=(
            "There is no way to score the readability of a region nobody identified. That "
            "case is Missing or Unreadable on other grounds, not a legibility question."
        ),
        evidence="api/pipeline/quality.py, fixtures/generator/layout.py",
        runs_in_production=False,
    ),
]


#: The exact change that would make the unwired capabilities run in the product.
#:
#: Written out rather than described because "wire up preprocessing" is not an instruction
#: anyone can act on, and because the ordering below is load-bearing: region legibility can
#: only be judged after extraction, since the regions come *from* the extractor.
WIRING = """\
In `api/routes/verify.py`, after ingest and quality scoring:

    from api.pipeline import preprocess as preprocess_mod

    prepared = await asyncio.to_thread(
        lambda: [preprocess_mod.preprocess(ingest_mod.to_array(i)) for i in ingested]
    )

Send the *preprocessed* pixels to the model rather than the ingested ones, so the
extractor sees the deskewed and light-corrected image and its bounding boxes are already
in the coordinate space the API contract declares (normalized against the
preprocessed image — see `BoundingBox` in `api/models.py`).

Then, after `_verify_within_budget` returns, force Unreadable on any field whose evidence
region nobody could read:

    for field_result in result.fields:
        evidence = field_result.evidence
        if evidence is None or evidence.bbox is None:
            continue
        region = quality_mod.assess_region(
            prepared[evidence.image_index].image, evidence.bbox
        )
        if not region.legible and field_result.verdict is not Verdict.UNREADABLE:
            field_result.verdict = Verdict.UNREADABLE
            field_result.extracted = None
            field_result.rationale = region.reason or "This part of the label could not be read."

Order matters and cannot be reversed: the regions come from the extractor, so the check
runs after it. That means it is a veto on the model's answer, not a way to avoid asking.

Two things to settle before applying it:

* **Fix `api/provider/fake.py` first.** It places the government warning at y 0.66-0.88,
  while the renderer puts it at 0.450-0.540 and everything below 0.62 is bare stock.
  Feeding those boxes to `assess_region` lands on blank label, which scores `blank`, which
  reads as legible — a false pass waiting for this wiring. The bands should come from
  `fixtures/generator/layout.py` rather than being restated. This wave does not own
  `api/provider/**`, so the mismatch is pinned instead by
  `tests/test_quality.py::test_the_fake_providers_evidence_bands_do_not_match_the_renderer`,
  which goes red once it is fixed. Any other provider supplying boxes needs the same check.
* Forcing a verdict after aggregation means the aggregate must be recomputed, or the
  recommendation can disagree with the rows it is derived from.
"""
