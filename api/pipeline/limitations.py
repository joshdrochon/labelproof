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
            "Detection fires on 4 of 15 robustness conditions for exactly this reason."
        ),
        why=(
            "There is nothing to detect. Inferring corners that are not in the frame would "
            "mean guessing where the label ends, and a wrong guess is a crop."
        ),
        evidence="scripts/crop_before_send.py, tests/test_deskew.py",
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
    ),
    Limitation(
        area="Lighting",
        handled=(
            "Underexposed photographs are lifted with local contrast, including the "
            "realistic case of a bottle lit from one side."
        ),
        not_handled=(
            "Images that are already well exposed are never touched, even where a "
            "particular region is hard to read."
        ),
        why=(
            "A warning printed pale grey on cream is a prominence violation (WARN-5, "
            "TC-06). Lifting contrast on every image would deliver that violation to the "
            "extractor looking perfectly legible and turn a real finding into a pass."
        ),
        evidence="tests/test_preprocess.py::test_a_buried_warning_keeps_its_low_contrast",
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
    ),
    Limitation(
        area="Blur",
        handled=(
            "Defocus and directional camera shake are both measured, on the worse of the "
            "two image axes. Small print goes illegible before large print does, which is "
            "why readability is judged per region."
        ),
        not_handled=(
            "Nothing is deconvolved or sharpened. A photograph past reading is pre-gated "
            "with a retake reason and zero model calls."
        ),
        why=(
            "Sharpening invents edges. The failure being avoided is not rejecting a bad "
            "photo — it is an extractor confidently returning plausible field values from "
            "mush."
        ),
        evidence="tests/test_robustness.py fabrication sweep, scripts/robustness_eval.py",
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
            "for the pipeline to be wrong. Tier B has not been captured yet."
        ),
        evidence="scripts/calibrate_quality.py, api/rules/thresholds.py",
    ),
    Limitation(
        area="The fixtures themselves",
        handled=(
            "Fifteen degradations, committed as files with digests, each declaring what "
            "the pipeline owes it, regenerable byte for byte."
        ),
        not_handled=(
            "They simulate optics, not physics. A Gaussian is not lens blur and an "
            "overlay is not a specular highlight on curved glass."
        ),
        why=(
            "Reproducibility, which regression tests require and photographs cannot give. "
            "The gap between this set and real bottles is Tier B's job, and the difference "
            "between the two numbers is the honest answer to 'does this work'."
        ),
        evidence="fixtures/robustness/manifest.json, BUILD.md §5",
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
    ),
]
