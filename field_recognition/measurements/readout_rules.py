"""Provider-independent grammar and supported measurement units."""

import re


NUMBER = r"[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)(?:[eE][+-]?\d+)?"
READOUT = re.compile(
    rf"^\s*{NUMBER}\s*(?:%|°?[CF]|℃|℉|[μµu]?g|kg|mg|ml|mL|L|rpm|r/min|"
    r"[mkM]?[AVW]|[kM]?Hz|Pa|kPa|MPa|bar|mm|cm|m|s|min|h|pH|ppm)?\s*$",
    re.I,
)
UNITS = {"温度": {"°C"}, "转速": {"rpm"}, "质量": {"g", "mg", "kg"}}


def fallback_reason(lines, local_error=None):
    """Return an evidence-based reason to review a local numeric candidate."""
    if local_error:
        return "local_ocr_error"
    if not lines:
        return "no_text"
    readings = [line for line in lines if READOUT.fullmatch(line["text"])]
    if not readings:
        return "no_numeric_readout"
    if all(re.fullmatch(r"\s*[+-]?0\d+\s*", line["text"]) for line in readings):
        return "decimal_uncertain"
    return None
