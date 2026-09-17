"""Screening contracts, not another model or a source-truth verifier.

A numeric score cannot override an explicit no-increment decision.  Missing
metadata is a format error, not evidence that a story has no value.
"""
import math

INCREMENT_KINDS = frozenset({
    "none", "asset", "parameter", "failure", "mechanism", "judgment", "capability",
})
SOURCE_STATES = frozenset({
    "observed", "self_report", "secondhand", "opinion", "rumor", "mixed", "unknown",
})


def finite_score(value, maximum=10):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 <= number <= maximum else None


def validate_increment(value):
    if not isinstance(value, dict):
        raise ValueError("invalid increment: object required")
    if not isinstance(value.get("kind"), str) or value["kind"] not in INCREMENT_KINDS:
        raise ValueError("invalid increment.kind")
    if not isinstance(value.get("source_status"), str) or value["source_status"] not in SOURCE_STATES:
        raise ValueError("invalid increment.source_status")
    out = {"kind": value["kind"], "source_status": value["source_status"]}
    for key in ("delta", "detail", "boundary"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f"invalid increment.{key}: nonempty string required")
        out[key] = value[key].strip()
    return out


def validate_candidates(items):
    """Use existing provider-format retry; never turn malformed JSON into skip."""
    for item in items:
        validate_increment(item.get("increment"))
        score = finite_score(item.get("signal_score"), maximum=5)
        if score is None or score < 1 or not score.is_integer():
            raise ValueError("invalid signal_score: integer 1-5 required")
    return items
