"""
vector_engine.py
~~~~~~~~~~~~~~~~~
Core image-to-vector conversion engine, built around the VTracer library
(classical/algorithmic tracing — no AI model involved, runs fully local).

Pipeline: analyze -> preprocess -> trace (VTracer) -> postprocess -> result.

The key insight that makes this engine "smart": the correct preprocessing
strategy depends entirely on what kind of image you're tracing. The engine
auto-detects the image class and routes each image through the right pipeline:

  STROKE_LINE_ART  — line drawings, icon sheets, sketches on a light/white
                     background (e.g. JPEG icon grids, scanned line art).
                     Problem: JPEG compression and anti-aliasing produce
                     gray fringe and background noise around strokes that
                     causes color-mode vtracer to invent dozens of spurious
                     gray "color regions" and trace them as garbage.
                     Fix: adaptive local-background subtraction to extract
                     pure stroke mask, then binary-mode trace.

  SOLID_MONOCHROME — logos, icons and illustrations with flat black/dark
                     fills on white, or very few colors. No JPEG fringe,
                     no complex color relationships.
                     Fix: simple global threshold -> binary or low-color trace.

  COLOR_ARTWORK    — multi-color logos, illustrations, photos-turned-vector.
                     Standard color-mode vtracer with existing preset params.
"""
import logging
import re
import tempfile
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image, ImageFilter, UnidentifiedImageError

from core.vector_presets import (
    VectorPreset, DEFAULT_PRESET_KEY, get_preset,
    FidelityLevel, DEFAULT_FIDELITY_KEY, get_fidelity,
)

logger = logging.getLogger(__name__)

try:
    import vtracer
    VTRACER_AVAILABLE = True
except ImportError:  # pragma: no cover
    vtracer = None
    VTRACER_AVAILABLE = False

# Images larger than this are downscaled before tracing (runtime control).
_MAX_TRACE_DIMENSION = 2000
# Images smaller than this are upscaled (curve-fitting precision).
_MIN_TRACE_DIMENSION = 400

_PATH_TAG_RE = re.compile(rb"<path[\s>]")


# ---------------------------------------------------------------------------
# Image class enum
# ---------------------------------------------------------------------------

class ImageClass(Enum):
    """Auto-detected content class, determines preprocessing pipeline."""
    STROKE_LINE_ART  = auto()   # thin strokes on light/white background
    SOLID_MONOCHROME = auto()   # flat-fill logo/icon, very few colors
    COLOR_ARTWORK    = auto()   # multi-color illustration / logo


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

class VectorizeResult:
    """
    Result of a VectorEngine.convert() call.
    Truthy iff ok.
    """

    __slots__ = (
        "ok", "reason", "svg_path", "path_count", "preset_key",
        "fidelity_key", "image_class", "source_size", "traced_size",
    )

    def __init__(
        self,
        ok: bool,
        reason: str = "",
        svg_path: Optional[Path] = None,
        path_count: int = 0,
        preset_key: str = "",
        fidelity_key: str = "",
        image_class: str = "",
        source_size: Optional[tuple] = None,
        traced_size: Optional[tuple] = None,
    ):
        self.ok = ok
        self.reason = reason
        self.svg_path = svg_path
        self.path_count = path_count
        self.preset_key = preset_key
        self.fidelity_key = fidelity_key
        self.image_class = image_class
        self.source_size = source_size
        self.traced_size = traced_size

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:
        if self.ok:
            return (
                f"VectorizeResult(ok=True, svg_path={self.svg_path!s}, "
                f"path_count={self.path_count}, preset={self.preset_key!r}, "
                f"class={self.image_class!r})"
            )
        return f"VectorizeResult(ok=False, reason={self.reason!r})"


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

@dataclass
class VectorizeOptions:
    """User/UI-facing knobs for a single conversion call."""

    preset: VectorPreset
    fidelity: str = DEFAULT_FIDELITY_KEY
    remove_background: bool = False
    background_tolerance: int = 24
    upscale_small_input: bool = True
    simplify_output: bool = True
    # When None the engine auto-detects. Set explicitly to override.
    force_image_class: Optional[ImageClass] = None
    overrides: dict = field(default_factory=dict)

    def resolved_fidelity(self) -> FidelityLevel:
        return get_fidelity(self.fidelity) or get_fidelity(DEFAULT_FIDELITY_KEY)

    def resolved_preset(self) -> VectorPreset:
        preset = self.resolved_fidelity().apply(self.preset)
        return preset.with_overrides(**self.overrides) if self.overrides else preset


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class VectorEngine:
    """
    Smart classical image-to-vector conversion engine.

    Usage:
        engine = VectorEngine()
        opts   = VectorizeOptions(preset=get_preset("icon"), fidelity="high")
        result = engine.convert("icon_sheet.jpg", "icon_sheet.svg", opts)
    """

    def __init__(self):
        if not VTRACER_AVAILABLE:
            logger.warning(
                "vtracer not installed — VectorEngine will fail at convert(). "
                "Run: pip install vtracer"
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def convert(
        self,
        input_path: str,
        output_path: str,
        options: Optional[VectorizeOptions] = None,
    ) -> VectorizeResult:
        """
        Convert a raster image to SVG. Returns VectorizeResult; never raises
        for expected failure modes (missing file, corrupt image, etc.).
        """
        if not VTRACER_AVAILABLE:
            return VectorizeResult(ok=False, reason="vtracer not installed (pip install vtracer).")

        if options is None:
            options = VectorizeOptions(preset=get_preset(DEFAULT_PRESET_KEY))

        src = Path(input_path)
        if not src.exists():
            return VectorizeResult(ok=False, reason=f"Input file not found: {src}")

        try:
            with Image.open(src) as img:
                img.load()
                source_size = img.size
                img_copy = img.copy()
        except UnidentifiedImageError:
            return VectorizeResult(ok=False, reason=f"Not a readable image: {src.name}")
        except OSError as exc:
            return VectorizeResult(ok=False, reason=f"Failed to open image: {exc}")

        # --- Step 1: classify the image ---
        image_class = (
            options.force_image_class
            if options.force_image_class is not None
            else self._classify_image(img_copy)
        )
        logger.debug("Image class for %s: %s", src.name, image_class.name)

        # --- Step 2: preprocess according to class + fidelity ---
        fidelity = options.resolved_fidelity()
        preset   = options.resolved_preset()

        try:
            if image_class == ImageClass.STROKE_LINE_ART:
                prepped, display_size, vtracer_kwargs = self._preprocess_stroke_line_art(
                    img_copy, options, fidelity, preset
                )
            elif image_class == ImageClass.SOLID_MONOCHROME:
                prepped, display_size, vtracer_kwargs = self._preprocess_solid_monochrome(
                    img_copy, options, fidelity, preset
                )
            else:
                prepped, display_size, vtracer_kwargs = self._preprocess_color_artwork(
                    img_copy, options, fidelity, preset
                )
        except Exception as exc:
            logger.error("Preprocessing failed for %s: %s", src.name, exc)
            return VectorizeResult(ok=False, reason=f"Preprocessing failed: {exc}")

        traced_size = prepped.size
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        # --- Step 3: trace ---
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = Path(tmp.name)

            self._save_for_vtracer(prepped, tmp_path)

            vtracer.convert_image_to_svg_py(str(tmp_path), str(out), **vtracer_kwargs)

        except Exception as exc:
            logger.error("VTracer failed for %s: %s", src.name, exc)
            return VectorizeResult(ok=False, reason=f"Trace failed: {exc}")
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

        if not out.exists() or out.stat().st_size == 0:
            return VectorizeResult(ok=False, reason="VTracer produced no output.")

        # --- Step 4: postprocess ---
        if display_size != traced_size:
            self._rescale_svg_viewport(out, traced_size, display_size)

        if options.simplify_output:
            self._optimize_svg(out)

        path_count = self._count_paths(out)
        logger.info(
            "Vectorized %s -> %s (%d paths, preset=%s, fidelity=%s, class=%s)",
            src.name, out.name, path_count, preset.key,
            fidelity.key, image_class.name,
        )

        return VectorizeResult(
            ok=True,
            svg_path=out,
            path_count=path_count,
            preset_key=preset.key,
            fidelity_key=fidelity.key,
            image_class=image_class.name,
            source_size=source_size,
            traced_size=traced_size,
        )

    def convert_batch(
        self,
        jobs: List[tuple],
        options: Optional[VectorizeOptions] = None,
    ) -> List[VectorizeResult]:
        results = []
        for job in jobs:
            if len(job) == 3:
                in_path, out_path, per_opts = job
            else:
                in_path, out_path = job
                per_opts = options
            results.append(self.convert(in_path, out_path, per_opts))
        return results

    # ------------------------------------------------------------------
    # Image classification
    # ------------------------------------------------------------------

    def _classify_image(self, img: Image.Image) -> ImageClass:
        """
        Examine pixel statistics to decide which preprocessing pipeline to use.

        Three signals are combined:
          1. Bimodality: high near-white + high near-black -> line-art family
          2. Stroke fill ratio: max dark run / image width
             - Short runs (<20%) -> thin strokes only -> STROKE_LINE_ART
             - Long runs (>20%) -> filled regions -> could be SOLID_MONOCHROME
          3. Mid-gray fraction: if >15% of pixels are in the 80-220 range
             the image has significant color content -> COLOR_ARTWORK
        """
        rgb = img.convert("RGB")
        arr = np.array(rgb).astype(np.float32)
        gray = arr.mean(axis=2).flatten()

        near_white   = float((gray > 220).sum()) / len(gray)
        dark_stroke  = float((gray < 80 ).sum()) / len(gray)
        mid_gray     = float(((gray >= 80) & (gray < 220)).sum()) / len(gray)
        bimodal      = near_white + dark_stroke

        # Strong color content -> color artwork regardless of bimodality
        if mid_gray > 0.15:
            return ImageClass.COLOR_ARTWORK

        # Not very bimodal (lots of in-between tones) -> color artwork too
        if bimodal < 0.80:
            return ImageClass.COLOR_ARTWORK

        # We're in line-art territory. Now discriminate stroke vs solid:
        # Sample rows to find the longest dark run as a fraction of image width.
        arr_gray = arr.mean(axis=2).astype(np.uint8)
        h, w = arr_gray.shape
        max_run = 0
        step = max(1, h // 40)       # sample ~40 rows for speed
        for row in arr_gray[::step]:
            run = 0
            for px in row:
                if px < 128:
                    run += 1
                    if run > max_run:
                        max_run = run
                else:
                    run = 0

        fill_ratio = max_run / w
        if fill_ratio < 0.20:
            # Thin strokes only — needs adaptive threshold pipeline
            return ImageClass.STROKE_LINE_ART
        else:
            return ImageClass.SOLID_MONOCHROME

    # ------------------------------------------------------------------
    # Preprocessing pipelines
    # ------------------------------------------------------------------

    def _preprocess_stroke_line_art(
        self,
        img: Image.Image,
        options: VectorizeOptions,
        fidelity: FidelityLevel,
        preset: VectorPreset,
    ) -> Tuple[Image.Image, tuple, dict]:
        """
        Pipeline for thin-stroke line art on a light/noisy background
        (e.g. JPEG-compressed icon sheets, scanned drawings).

        The core problem this solves: JPEG compression introduces a soft
        anti-aliased gray fringe around every stroke AND makes the background
        slightly off-white. If you feed this directly to vtracer's color mode,
        it invents a separate color region for each gray fringe zone and traces
        them as stray filled polygons — producing complete garbage.

        Solution:
          1. Convert to grayscale
          2. Estimate the LOCAL background luminance using a large Gaussian blur
             (this handles uneven lighting and gradient backgrounds too)
          3. Threshold: pixels that are darker than their local neighborhood by
             more than `sensitivity` are strokes; everything else is background
          4. Noise cleanup: small MedianFilter removes isolated JPEG artifact
             pixels that survived the threshold
          5. Output: clean B&W binary image to vtracer in BINARY mode
             -> vtracer traces pure contours, no color confusion possible
        """
        gray = img.convert("L")

        # Adaptive threshold sensitivity scales with fidelity:
        # - Low fidelity: more aggressive (only capture main strokes)
        # - High/Ultra: sensitive (capture fine hairlines too)
        sensitivity_map = {"low": 35, "standard": 25, "high": 18, "ultra": 12}
        sensitivity = sensitivity_map.get(fidelity.key, 25)

        # Gaussian blur radius: large enough to span inter-stroke gaps
        # but not so large it bleeds across major image regions.
        # For fidelity level high/ultra, use a larger radius for better
        # local-background modeling (helps with uneven scan illumination).
        blur_radius = 15 if fidelity.key in ("low", "standard") else 20

        arr = np.array(gray).astype(np.float32)
        blurred_arr = np.array(gray.filter(ImageFilter.GaussianBlur(blur_radius))).astype(np.float32)

        # Stroke mask: pixels significantly darker than local background
        stroke_mask = (blurred_arr - arr) > sensitivity

        # Noise removal: removes isolated JPEG artifact pixels
        # MedianFilter size scales with fidelity (less aggressive at high fidelity
        # to preserve fine hairlines like fingerprint ridges)
        noise_kernel = 3 if fidelity.key in ("high", "ultra") else 5
        binary = Image.fromarray(stroke_mask.astype(np.uint8) * 255, mode="L")
        cleaned = binary.filter(ImageFilter.MedianFilter(noise_kernel))

        # Convert to dark-on-white (vtracer convention: dark = content)
        for_trace = Image.fromarray(255 - np.array(cleaned), mode="L").convert("RGB")
        for_trace = for_trace.convert("RGBA")

        # Resize for trace (note: no supersample needed here — the adaptive
        # threshold already produces a clean binary mask at native resolution,
        # and supersampling a binary image doesn't add information)
        for_trace = self._resize_for_trace(for_trace, options)
        display_size = for_trace.size

        # Build vtracer kwargs optimized for binary stroke tracing
        # corner_threshold: higher = smoother curves (good for icon outlines)
        # filter_speckle: removes any residual noise islands
        # length_threshold: low = preserve fine detail (fingerprint lines etc)
        vtracer_kwargs = {
            "colormode":        "binary",
            "hierarchical":     "stacked",
            "mode":             "spline",
            "filter_speckle":   max(2, preset.filter_speckle - 2),
            "corner_threshold": preset.corner_threshold,
            "length_threshold": min(2.0, preset.length_threshold),
            "splice_threshold": preset.splice_threshold,
            "path_precision":   preset.path_precision,
            "max_iterations":   preset.max_iterations,
        }

        return for_trace, display_size, vtracer_kwargs

    def _preprocess_solid_monochrome(
        self,
        img: Image.Image,
        options: VectorizeOptions,
        fidelity: FidelityLevel,
        preset: VectorPreset,
    ) -> Tuple[Image.Image, tuple, dict]:
        """
        Pipeline for flat-fill logos, solid icons, and monochrome artwork.
        These have large filled dark regions, not just thin strokes, so they
        don't need adaptive thresholding — a simple global luminance threshold
        works well and preserves the fill regions as solid paths.
        """
        img = img.convert("RGBA")

        if options.remove_background:
            img = self._flood_remove_background(img, options.background_tolerance)

        img = self._resize_for_trace(img, options)
        display_size = img.size

        if fidelity.denoise_radius > 0:
            img = self._denoise(img, fidelity.denoise_radius)

        if fidelity.supersample > 1.0:
            w, h = img.size
            new_size = (round(w * fidelity.supersample), round(h * fidelity.supersample))
            img = img.resize(new_size, Image.LANCZOS)
            img = self._denoise(img, max(3, fidelity.denoise_radius))

        vtracer_kwargs = preset.to_vtracer_kwargs()
        # Force binary for clean solid artwork — color mode would invent
        # gray "shadow" regions from AA edges around dark fills
        vtracer_kwargs["colormode"] = "binary"

        return img, display_size, vtracer_kwargs

    def _preprocess_color_artwork(
        self,
        img: Image.Image,
        options: VectorizeOptions,
        fidelity: FidelityLevel,
        preset: VectorPreset,
    ) -> Tuple[Image.Image, tuple, dict]:
        """
        Pipeline for multi-color logos, illustrations, and color artwork.
        Uses the full color-mode trace with existing preset parameters.
        """
        img = img.convert("RGBA")

        if options.remove_background:
            img = self._flood_remove_background(img, options.background_tolerance)

        img = self._resize_for_trace(img, options)
        display_size = img.size

        if fidelity.denoise_radius > 0:
            img = self._denoise(img, fidelity.denoise_radius)

        if fidelity.supersample > 1.0:
            w, h = img.size
            new_size = (round(w * fidelity.supersample), round(h * fidelity.supersample))
            img = img.resize(new_size, Image.LANCZOS)
            img = self._denoise(img, max(3, fidelity.denoise_radius))

        return img, display_size, preset.to_vtracer_kwargs()

    # ------------------------------------------------------------------
    # Shared preprocessing helpers
    # ------------------------------------------------------------------

    def _save_for_vtracer(self, img: Image.Image, path: Path) -> None:
        """
        Composite the preprocessed image onto solid white before saving.
        Without this, semi-transparent pixels (from LANCZOS resize after
        background removal) are treated by vtracer as a third "color region"
        — multiplying paths and producing broken output.
        """
        if img.mode == "RGBA":
            white = Image.new("RGBA", img.size, (255, 255, 255, 255))
            white.paste(img, mask=img.getchannel("A"))
            white.convert("RGB").save(path, format="PNG")
        else:
            img.convert("RGB").save(path, format="PNG")

    def _denoise(self, img: Image.Image, radius: int) -> Image.Image:
        """Edge-preserving median denoise. Handles RGBA cleanly."""
        if radius % 2 == 0:
            radius += 1
        rgb   = img.convert("RGB").filter(ImageFilter.MedianFilter(size=radius))
        alpha = img.getchannel("A").filter(ImageFilter.MedianFilter(size=radius))
        rgb.putalpha(alpha)
        return rgb

    def _resize_for_trace(self, img: Image.Image, options: VectorizeOptions) -> Image.Image:
        w, h    = img.size
        longest = max(w, h)

        if longest > _MAX_TRACE_DIMENSION:
            scale    = _MAX_TRACE_DIMENSION / longest
            new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
            return img.resize(new_size, Image.LANCZOS)

        if options.upscale_small_input and longest < _MIN_TRACE_DIMENSION:
            scale    = _MIN_TRACE_DIMENSION / longest
            new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
            return img.resize(new_size, Image.NEAREST)

        return img

    def _flood_remove_background(self, img: Image.Image, tolerance: int) -> Image.Image:
        """
        Make corner-sampled background pixels transparent. Fast heuristic
        for logos/icons on flat solid backgrounds.
        """
        rgba   = img.convert("RGBA")
        pixels = rgba.load()
        w, h   = rgba.size

        corners = [pixels[0, 0], pixels[w - 1, 0], pixels[0, h - 1], pixels[w - 1, h - 1]]
        bg      = max(set(corners), key=corners.count)

        def close(p):
            return (
                abs(p[0] - bg[0]) <= tolerance
                and abs(p[1] - bg[1]) <= tolerance
                and abs(p[2] - bg[2]) <= tolerance
            )

        updated = [
            (r, g, b, 0) if close((r, g, b, a)) else (r, g, b, a)
            for (r, g, b, a) in rgba.getdata()
        ]
        rgba.putdata(updated)
        return rgba

    # ------------------------------------------------------------------
    # Postprocessing
    # ------------------------------------------------------------------

    def _count_paths(self, svg_path: Path) -> int:
        try:
            return len(_PATH_TAG_RE.findall(svg_path.read_bytes()))
        except OSError:
            return 0

    def _rescale_svg_viewport(self, svg_path: Path, traced_size: tuple, display_size: tuple) -> None:
        """
        After supersampled tracing, scale the SVG viewport back to display_size
        via a viewBox — path coordinates are unchanged, visual display is correct.
        """
        try:
            text = svg_path.read_text(encoding="utf-8")
        except OSError:
            return

        tw, th = traced_size
        dw, dh = display_size

        if "viewBox" not in text:
            text = re.sub(
                r"(<svg\b[^>]*?)(\swidth=\"[^\"]*\"\s+height=\"[^\"]*\")",
                lambda m: f'{m.group(1)} viewBox="0 0 {tw} {th}"{m.group(2)}',
                text, count=1,
            )

        text = re.sub(r'width="[^"]*"',  f'width="{dw}"',  text, count=1)
        text = re.sub(r'height="[^"]*"', f'height="{dh}"', text, count=1)

        try:
            svg_path.write_text(text, encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not rescale SVG viewport for %s: %s", svg_path.name, exc)

    def _optimize_svg(self, svg_path: Path) -> None:
        """
        Strip VTracer generator comment and collapse redundant whitespace.
        Never rewrites path geometry.

        NOTE: scour was evaluated and removed — it corrupts vtracer's
        stacked SVGs that combine viewBox with per-path transform=translate().
        """
        try:
            text = svg_path.read_text(encoding="utf-8")
        except OSError:
            return

        text = re.sub(r"<!--.*?-->\s*", "", text, flags=re.DOTALL)
        text = re.sub(r">\s+<", "><", text)

        try:
            svg_path.write_text(text, encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not clean SVG for %s: %s", svg_path.name, exc)
