"""Apple Vision OCR bridge (lazy PyObjC import for macOS)."""

from __future__ import annotations

import tempfile
from pathlib import Path

_vision_cache: dict = {}


def _vision_init() -> dict:
    """Lazy-import PyObjC Vision/Quartz/Cocoa bindings on first OCR call."""
    if "Vision" in _vision_cache:
        return _vision_cache
    import Quartz
    import Vision
    from Cocoa import NSURL

    _vision_cache["Vision"] = Vision
    _vision_cache["Quartz"] = Quartz
    _vision_cache["NSURL"] = NSURL
    return _vision_cache


def ocr_image(img, lang: str = "en-US") -> str:
    """Run Apple Vision accurate text recognition on a PIL.Image."""
    v = _vision_init()
    Vision = v["Vision"]
    Quartz = v["Quartz"]
    NSURL = v["NSURL"]

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp = Path(f.name)
    try:
        from PIL import Image

        bg = Image.new("RGB", img.size, (0, 0, 0))
        if img.mode == "RGBA":
            bg.paste(img, mask=img.split()[3])
        else:
            bg.paste(img)
        bg.save(tmp, "PNG")

        url = NSURL.fileURLWithPath_(str(tmp))
        src = Quartz.CGImageSourceCreateWithURL(url, None)
        if src is None:
            return ""
        cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        if cg is None:
            return ""

        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(1)  # accurate
        req.setUsesLanguageCorrection_(True)
        req.setRecognitionLanguages_([lang])

        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
            cg, None
        )
        ok, _err = handler.performRequests_error_([req], None)
        if not ok:
            return ""
        lines = []
        for r in req.results() or []:
            cands = r.topCandidates_(1)
            if cands:
                lines.append(str(cands[0].string()))
        return "\n".join(lines).strip()
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
