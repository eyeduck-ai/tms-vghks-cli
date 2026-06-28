from __future__ import annotations

import sys
import types
from typing import Any

from . import cli_impl as _impl


def build_parser():
    return _impl.build_parser()


def main(argv: list[str] | None = None) -> int:
    return _impl.main(argv)


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *dir(_impl)})


class _CliFacadeModule(types.ModuleType):
    def __setattr__(self, name: str, value: Any) -> None:
        if name not in {"_impl", "_CliFacadeModule"} and hasattr(_impl, name):
            setattr(_impl, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _CliFacadeModule

__all__ = ["build_parser", "main"]
