"""Resolve frozen repository references without rewriting scientific inputs."""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LEGACY_MANIFEST_ROOT = Path("v2/config")
MANIFEST_ROOT = Path("experiments/thesis/manifests")
LEGACY_WAVE_B_PREREGISTRATION = Path(
    "v2/docs/wave-b-final-analysis-preregistration.md"
)
WAVE_B_PREREGISTRATION = Path(
    "experiments/thesis/preregistration/wave-b-final-analysis-preregistration.md"
)


def resolve_repository_reference(path: Path | str) -> Path:
    """Map known frozen logical paths while leaving arbitrary fixture paths alone."""
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            logical = candidate.relative_to(REPOSITORY_ROOT)
        except ValueError:
            return candidate
    else:
        logical = candidate
    if logical == LEGACY_WAVE_B_PREREGISTRATION:
        return REPOSITORY_ROOT / WAVE_B_PREREGISTRATION
    if logical == LEGACY_MANIFEST_ROOT:
        return REPOSITORY_ROOT / MANIFEST_ROOT
    if logical.is_relative_to(LEGACY_MANIFEST_ROOT):
        return REPOSITORY_ROOT / MANIFEST_ROOT / logical.relative_to(LEGACY_MANIFEST_ROOT)
    if logical.is_relative_to(MANIFEST_ROOT) or logical == WAVE_B_PREREGISTRATION:
        return REPOSITORY_ROOT / logical
    return candidate
