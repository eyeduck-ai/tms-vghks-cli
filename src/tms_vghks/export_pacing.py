from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol


DEFAULT_EXPORT_DELAY_MIN_MS = 400
DEFAULT_EXPORT_DELAY_MAX_MS = 1400


@dataclass(slots=True)
class ExportPacingOptions:
    min_ms: int = DEFAULT_EXPORT_DELAY_MIN_MS
    max_ms: int = DEFAULT_EXPORT_DELAY_MAX_MS
    enabled: bool = True
    seed: int | None = None


@dataclass(slots=True)
class ExportPacer:
    options: ExportPacingOptions = field(default_factory=ExportPacingOptions)
    sleep_func: Callable[[float], None] = time.sleep
    sleep_count: int = 0
    total_sleep_seconds: float = 0.0
    label_counts: dict[str, int] = field(default_factory=dict)
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.options.seed)
        _validate_export_pacing_options(self.options)

    def sleep(self, label: str = "") -> float:
        if not self.options.enabled or self.options.max_ms <= 0:
            return 0.0
        delay_ms = self._rng.uniform(self.options.min_ms, self.options.max_ms)
        seconds = max(0.0, delay_ms / 1000.0)
        if seconds <= 0:
            return 0.0
        self.sleep_func(seconds)
        self.sleep_count += 1
        self.total_sleep_seconds += seconds
        label_key = label or "unlabeled"
        self.label_counts[label_key] = self.label_counts.get(label_key, 0) + 1
        return seconds

    def summary(self) -> dict[str, int | float | bool | None | dict[str, int]]:
        return {
            "enabled": bool(self.options.enabled and self.options.max_ms > 0),
            "min_ms": self.options.min_ms,
            "max_ms": self.options.max_ms,
            "seed": self.options.seed,
            "sleep_count": self.sleep_count,
            "total_sleep_seconds": round(self.total_sleep_seconds, 3),
            "label_counts": dict(sorted(self.label_counts.items())),
        }


class ExportPacerProtocol(Protocol):
    def sleep(self, label: str = "") -> float:
        ...

    def summary(self) -> dict[str, object]:
        ...


def disabled_export_pacing_options() -> ExportPacingOptions:
    return ExportPacingOptions(min_ms=0, max_ms=0, enabled=False)


def export_pacing_options_from_cli(
    *,
    delay_min_ms: int | None,
    delay_max_ms: int | None,
    no_random_delay: bool = False,
    delay_seed: int | None = None,
) -> ExportPacingOptions:
    if no_random_delay:
        return ExportPacingOptions(min_ms=0, max_ms=0, enabled=False, seed=delay_seed)
    options = ExportPacingOptions(
        min_ms=DEFAULT_EXPORT_DELAY_MIN_MS if delay_min_ms is None else delay_min_ms,
        max_ms=DEFAULT_EXPORT_DELAY_MAX_MS if delay_max_ms is None else delay_max_ms,
        enabled=True,
        seed=delay_seed,
    )
    _validate_export_pacing_options(options)
    return options


def make_export_pacer(
    options: ExportPacingOptions | None,
    *,
    sleep_func: Callable[[float], None] = time.sleep,
) -> ExportPacer:
    return ExportPacer(options or disabled_export_pacing_options(), sleep_func=sleep_func)


def _validate_export_pacing_options(options: ExportPacingOptions) -> None:
    if options.min_ms < 0 or options.max_ms < 0:
        raise ValueError("export delay values must be non-negative")
    if options.min_ms > options.max_ms:
        raise ValueError("export delay min must be less than or equal to max")


__all__ = [
    "DEFAULT_EXPORT_DELAY_MAX_MS",
    "DEFAULT_EXPORT_DELAY_MIN_MS",
    "ExportPacer",
    "ExportPacerProtocol",
    "ExportPacingOptions",
    "disabled_export_pacing_options",
    "export_pacing_options_from_cli",
    "make_export_pacer",
]
