"""Abstract listing source adapter."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterator


class ListingAdapter(ABC):
    """Anything that knows how to yield listing dicts ready for upsert_listing()."""

    name: str = "base"

    @abstractmethod
    def iter_listings(self) -> Iterator[dict[str, Any]]:
        ...
