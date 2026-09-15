"""Shared restore records used by candidate and physical-archive workflows."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FingerprintComparison:
    exact_core_passed: bool
    physical_statistics_passed: bool
    exact_mismatches: dict[str, dict[str, Any]]
    physical_mismatches: dict[str, dict[str, Any]]

    @property
    def passed(self) -> bool:
        return self.exact_core_passed and self.physical_statistics_passed


@dataclass(frozen=True)
class CandidateBaseline:
    baseline_id: uuid.UUID
    preflight_id: uuid.UUID
    validation_id: uuid.UUID
    restore_mechanism: str
    approved: bool
    exact_core: dict[str, Any]
    physical_statistics: dict[str, Any]
    physical_tolerances: dict[str, Any]


@dataclass(frozen=True)
class CandidateRestoreResult:
    restore_id: uuid.UUID
    trial_id: uuid.UUID
    baseline_id: uuid.UUID
    validation_id: uuid.UUID
    attempt: int
    restored: bool
    comparison: FingerprintComparison
    duration_seconds: float
