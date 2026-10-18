"""The dated nominal price list (backend/config/prices.yaml), shared by the feed and the pages.

The feed prices a call from its tokens only when LiteLLM recorded no spend for it; the pages show
the list's source and date beside every cost figure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path

import yaml

PRICES_PATH = Path(__file__).resolve().parent / "config" / "prices.yaml"


@dataclass(frozen=True)
class Price:
    provider_model: str
    priced_as: str
    input_per_token: float
    output_per_token: float


@dataclass(frozen=True)
class PriceList:
    source: str
    fetched_on: date
    currency: str
    tagging_started_at: datetime
    prices: tuple[Price, ...]

    def find(self, provider_model: str | None) -> Price | None:
        """The price for a model, matched with or without LiteLLM's `openai/` prefix."""
        if not provider_model:
            return None
        bare = provider_model.removeprefix("openai/")
        for p in self.prices:
            if p.provider_model.removeprefix("openai/") == bare:
                return p
        return None

    def ref(self, p: Price) -> str:
        """How a stored call names the price it was given: 'qwen/qwen3.8-flash, OpenRouter 2026-10-17'."""
        return f"{p.priced_as}, OpenRouter {self.fetched_on.isoformat()}"

    @property
    def label(self) -> str:
        """The words shown beside every cost figure."""
        names = ", ".join(sorted({p.priced_as for p in self.prices}))
        day = f"{self.fetched_on.day} {self.fetched_on.strftime('%b %Y')}"
        return f"Nominal: OpenRouter price for {names} on {day}"


def _parse_dt(text: str) -> datetime:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_prices(raw: dict[str, object]) -> PriceList:
    rows = raw.get("prices") or []
    assert isinstance(rows, list)
    prices = tuple(
        Price(provider_model=str(r["provider_model"]), priced_as=str(r["priced_as"]),
              input_per_token=float(r["input_per_token"]), output_per_token=float(r["output_per_token"]))
        for r in rows
    )
    return PriceList(
        source=str(raw.get("source", "")), fetched_on=date.fromisoformat(str(raw["fetched_on"])),
        currency=str(raw.get("currency", "USD")), tagging_started_at=_parse_dt(str(raw["tagging_started_at"])),
        prices=prices,
    )


@lru_cache(maxsize=4)
def load_prices(path: Path = PRICES_PATH) -> PriceList:
    return parse_prices(yaml.safe_load(path.read_text(encoding="utf-8")))
