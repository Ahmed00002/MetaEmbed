"""
vector_presets.py
~~~~~~~~~~~~~~~~~~
Tuned parameter sets for core/vector_engine.py (VTracer backend).

Rather than exposing raw VTracer parameters directly to the user, we ship
curated presets tuned for the specific content types our users actually
upload (logos, icons, line art). Power users can still override any field
via VectorPreset.with_overrides(), which the "Advanced" panel will use.

VTracer parameter notes (for future maintainers):
- colormode:        "color" | "binary" (binary = black & white only)
- mode:              "spline" (smooth curves, best for logos/illustrations)
                      | "polygon" (sharp straight segments)
                      | "none" (pixel-stair-stepped, rarely useful)
- filter_speckle:    int, px^2 area below which a region is discarded as
                      noise. Higher = cleaner output, fewer stray paths.
- color_precision:   int (1-8), bits of color quantization. Lower = fewer
                      distinct colors = smaller/cleaner SVG.
- layer_difference:  int, min color distance between layers when
                      hierarchical clustering colors. Higher = fewer layers.
- corner_threshold:  int (0-180) degrees. Lower = more corners detected
                      (sharper logos); higher = smoother/rounder curves.
- length_threshold:  float, minimum segment length kept after simplification.
- splice_threshold:  int (0-180) degrees, controls where curves are spliced
                      into separate Bezier segments.
- path_precision:    int, decimal places in output path coordinates.
"""
from dataclasses import dataclass, replace
from typing import Dict, Optional


@dataclass(frozen=True)
class VectorPreset:
    """One named, tuned parameter set for VTracer."""

    key: str
    label: str
    description: str

    colormode: str = "color"
    hierarchical: str = "stacked"
    mode: str = "spline"
    filter_speckle: int = 4
    color_precision: int = 6
    layer_difference: int = 16
    corner_threshold: int = 60
    length_threshold: float = 4.0
    max_iterations: int = 10
    splice_threshold: int = 45
    path_precision: int = 3

    def with_overrides(self, **kwargs) -> "VectorPreset":
        """Return a copy of this preset with specific fields overridden
        (used by the Advanced panel when the user nudges a slider)."""
        return replace(self, **kwargs)

    def to_vtracer_kwargs(self) -> Dict:
        """VTracer's Python binding kwargs, derived from this preset."""
        return {
            "colormode": self.colormode,
            "hierarchical": self.hierarchical,
            "mode": self.mode,
            "filter_speckle": self.filter_speckle,
            "color_precision": self.color_precision,
            "layer_difference": self.layer_difference,
            "corner_threshold": self.corner_threshold,
            "length_threshold": self.length_threshold,
            "max_iterations": self.max_iterations,
            "splice_threshold": self.splice_threshold,
            "path_precision": self.path_precision,
        }


# ----------------------------------------------------------------------
# Curated presets
# ----------------------------------------------------------------------

LOGO_FLAT_COLOR = VectorPreset(
    key="logo_flat",
    label="Logo / Flat Color",
    description=(
        "Best for logos, badges, and flat-color brand marks. Aggressively "
        "quantizes colors and favors crisp corners over smooth curves, "
        "producing the smallest, cleanest path count."
    ),
    colormode="color",
    mode="spline",
    filter_speckle=8,
    color_precision=5,
    layer_difference=20,
    corner_threshold=40,
    length_threshold=4.0,
    splice_threshold=45,
    path_precision=2,
)

ICON = VectorPreset(
    key="icon",
    label="Icon",
    description=(
        "Tuned for small, simple icon artwork. Most aggressive speckle "
        "filtering and color reduction to keep file size minimal while "
        "icons still look crisp at small render sizes."
    ),
    colormode="color",
    mode="spline",
    filter_speckle=10,
    color_precision=4,
    layer_difference=24,
    corner_threshold=35,
    length_threshold=4.0,
    splice_threshold=45,
    path_precision=2,
)

LINE_ART = VectorPreset(
    key="line_art",
    label="Line Art / Sketch",
    description=(
        "Best for black-and-white sketches, hand-drawn line art, and "
        "single-color illustrations. Preserves thin strokes and fine "
        "detail rather than flattening them away."
    ),
    colormode="binary",
    mode="spline",
    filter_speckle=2,
    color_precision=6,
    layer_difference=8,
    corner_threshold=80,
    length_threshold=2.0,
    splice_threshold=60,
    path_precision=4,
)

DETAILED_ILLUSTRATION = VectorPreset(
    key="detailed",
    label="Detailed Illustration",
    description=(
        "For multi-color illustrations with gradients or many distinct "
        "regions where fidelity to the source matters more than minimal "
        "path count."
    ),
    colormode="color",
    mode="spline",
    filter_speckle=2,
    color_precision=8,
    layer_difference=8,
    corner_threshold=70,
    length_threshold=3.0,
    splice_threshold=45,
    path_precision=4,
)

PRESETS: Dict[str, VectorPreset] = {
    p.key: p
    for p in (LOGO_FLAT_COLOR, ICON, LINE_ART, DETAILED_ILLUSTRATION)
}

DEFAULT_PRESET_KEY = LOGO_FLAT_COLOR.key


def get_preset(key: str) -> Optional[VectorPreset]:
    return PRESETS.get(key)


# ----------------------------------------------------------------------
# Fidelity levels — Illustrator "Image Trace" / Vector Magic style
# ----------------------------------------------------------------------
#
# Content presets above answer "what kind of artwork is this" (logo, icon,
# line art...). Fidelity answers an orthogonal question: "how hard should
# the engine work to match the source exactly". This mirrors Illustrator's
# Low/High Fidelity Photo presets and Vector Magic's Low/Medium/High/
# Highest detail slider.
#
# The accuracy gains here are NOT just "turn the precision numbers up" —
# the dominant source of visible quality loss (wobbly, slightly-off-straight
# edges around anti-aliased source pixels) comes from VTracer's color
# quantizer making inconsistent per-pixel decisions along soft/AA'd edges.
# The fix that actually matters is denoising those edges *before* tracing
# (an edge-preserving median filter — flattens the AA gradient into a
# clean boundary without blurring real geometry) combined with raising
# color_precision so the quantizer doesn't need to guess. This was verified
# empirically: identical trace parameters produce a visibly wobbly curve
# on a raw anti-aliased source and a clean, straight one once the median
# denoise pass runs first.
#
# - denoise_radius:  PIL MedianFilter kernel size (must be odd). 0 = off.
# - supersample:     factor to upscale the source before tracing, then the
#                     traced SVG's viewBox is scaled back down so output
#                     dimensions are unchanged but curve fitting had more
#                     sub-pixel data to work with. Only used at "ultra" —
#                     in testing, aggressive supersampling combined with
#                     the wrong corner_threshold can introduce its own
#                     faceting artifacts, so it's applied conservatively
#                     (2x, not 4x+) and only when it has a real corner_
#                     threshold pairing tuned for it.
# - param overrides: deltas applied on top of whatever content preset is
#                     selected, then clamped to VTracer's valid ranges.

@dataclass(frozen=True)
class FidelityLevel:
    key: str
    label: str
    description: str

    denoise_radius: int = 0
    supersample: float = 1.0

    color_precision_delta: int = 0
    filter_speckle_delta: int = 0          # additive; can be negative
    length_threshold_mult: float = 1.0
    corner_threshold_delta: int = 0
    path_precision_delta: int = 0
    max_iterations: int = 10
    splice_threshold_delta: int = 0

    def apply(self, preset: VectorPreset) -> VectorPreset:
        """Return a new VectorPreset with this fidelity level's deltas
        applied on top of the given content preset, clamped to VTracer's
        valid parameter ranges."""
        return preset.with_overrides(
            color_precision=_clamp(preset.color_precision + self.color_precision_delta, 1, 8),
            filter_speckle=_clamp(preset.filter_speckle + self.filter_speckle_delta, 0, 64),
            length_threshold=max(0.1, preset.length_threshold * self.length_threshold_mult),
            corner_threshold=_clamp(preset.corner_threshold + self.corner_threshold_delta, 0, 180),
            path_precision=_clamp(preset.path_precision + self.path_precision_delta, 0, 8),
            max_iterations=self.max_iterations,
            splice_threshold=_clamp(preset.splice_threshold + self.splice_threshold_delta, 0, 180),
        )


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


FIDELITY_LOW = FidelityLevel(
    key="low",
    label="Low Fidelity (Fastest)",
    description=(
        "Maximum simplification — fewest anchor points and smallest file "
        "size. Good for quick previews or extremely simple marks. May "
        "round off fine detail."
    ),
    denoise_radius=0,
    supersample=1.0,
    color_precision_delta=-2,
    filter_speckle_delta=+6,
    length_threshold_mult=2.0,
    corner_threshold_delta=0,
    path_precision_delta=-1,
    max_iterations=6,
)

FIDELITY_STANDARD = FidelityLevel(
    key="standard",
    label="Standard",
    description=(
        "Balanced default — matches the source closely with a reasonable "
        "path count. Good general-purpose choice."
    ),
    denoise_radius=0,
    supersample=1.0,
    color_precision_delta=0,
    filter_speckle_delta=0,
    length_threshold_mult=1.0,
    corner_threshold_delta=0,
    path_precision_delta=0,
    max_iterations=10,
)

FIDELITY_HIGH = FidelityLevel(
    key="high",
    label="High Fidelity",
    description=(
        "Denoises anti-aliased source edges before tracing and raises "
        "color/curve precision so edges that should be straight come out "
        "straight instead of wobbly. Slower, recommended for final "
        "stock-marketplace delivery."
    ),
    denoise_radius=3,
    supersample=1.0,
    color_precision_delta=+2,
    filter_speckle_delta=-2,
    length_threshold_mult=0.5,
    corner_threshold_delta=0,
    path_precision_delta=+1,
    max_iterations=20,
    splice_threshold_delta=-5,
)

FIDELITY_ULTRA = FidelityLevel(
    key="ultra",
    label="Ultra (Maximum Accuracy)",
    description=(
        "Strongest denoise pass plus 2x supersampling for sub-pixel edge "
        "accuracy on top of High Fidelity's settings. Slowest option — "
        "use when the output must be as close to vector-perfect as "
        "possible, e.g. final stock-marketplace delivery of a hero asset."
    ),
    denoise_radius=5,
    supersample=2.0,
    color_precision_delta=+2,
    filter_speckle_delta=-3,
    length_threshold_mult=0.35,
    # At 2x supersampled resolution, a content preset's corner_threshold
    # (tuned for the original pixel grid) becomes too aggressive — curve
    # segments that were one smooth corner now span twice the boundary
    # points, and a low threshold misreads that as many small corners,
    # shattering curves into stray polygon facets. Verified empirically:
    # without this boost, Ultra fidelity on an icon-style preset produced
    # broken, fragmented output; +35 restored clean smooth geometry.
    corner_threshold_delta=+35,
    path_precision_delta=+2,
    max_iterations=30,
    splice_threshold_delta=-5,
)

FIDELITY_LEVELS: Dict[str, FidelityLevel] = {
    f.key: f for f in (FIDELITY_LOW, FIDELITY_STANDARD, FIDELITY_HIGH, FIDELITY_ULTRA)
}

DEFAULT_FIDELITY_KEY = FIDELITY_STANDARD.key


def get_fidelity(key: str) -> Optional[FidelityLevel]:
    return FIDELITY_LEVELS.get(key)
