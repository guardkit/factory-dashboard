"""A stand-in for LiteLLM's spend log, for tests: rows in LiteLLM's own shape, read the way the
Postgres source reads them (keyset pages ordered by start time and request id).

Rows can be hidden (written late by LiteLLM) and the source can be made to fail (an outage).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backend.projector.cost_import import SpendRow, import_once

PRICED = "openai/qwen3.8-flash-next-autoround"
IN_PRICE, OUT_PRICE = 1.5e-07, 4.7e-07


class SourceDown(Exception):
    """What the driver raises when the database cannot be reached (the message quotes the address)."""


class FakeSpend:
    def __init__(self, t0: datetime) -> None:
        self.t0 = t0
        self.rows: list[SpendRow] = []
        self.hidden: set[str] = set()
        self.down = False
        self.reads = 0

    def add(self, request_id: str, minute: float, tag: str | None = None, *, prompt: int = 1000,
            completion: int = 100, spend: float | None = None, model_name: str = "flash-next-t06",
            provider_model: str = PRICED, status: str = "success", duration_min: float = 0.5,
            hidden: bool = False) -> SpendRow:
        start = self.t0 + timedelta(minutes=minute)
        if spend is None:
            spend = prompt * IN_PRICE + completion * OUT_PRICE if provider_model == PRICED else 0.0
        tags = ["User-Agent: AsyncOpenAI"] + ([f"x-feature-id: {tag}"] if tag else [])
        row = SpendRow(request_id=request_id, start=start, end=start + timedelta(minutes=duration_min),
                       model_name=model_name, provider_model=provider_model, key_alias="factory-key", tags=tags,
                       status=status, prompt_tokens=prompt, completion_tokens=completion, spend=spend)
        self.rows.append(row)
        if hidden:
            self.hidden.add(request_id)
        return row

    def reveal(self, request_id: str) -> None:
        self.hidden.discard(request_id)

    def _visible(self) -> list[SpendRow]:
        return sorted((r for r in self.rows if r.request_id not in self.hidden), key=lambda r: (r.start, r.request_id))

    def rows_after(self, start: datetime, after_id: str, limit: int,
                   before: datetime | None = None) -> list[SpendRow]:
        if self.down:
            raise SourceDown('connection to server at "postgresql://reader:not-a-real-password@192.0.2.9:5435" failed')
        self.reads += 1
        out = [r for r in self._visible() if (r.start, r.request_id) > (start, after_id)
               and (before is None or r.start < before)]
        return out[:limit]

    def counts_by_day(self, since: datetime) -> dict[str, int]:
        if self.down:
            raise SourceDown("down")
        out: dict[str, int] = {}
        for r in self._visible():
            if r.start >= since:
                day = r.start.astimezone(UTC).date().isoformat()
                out[day] = out.get(day, 0) + 1
        return out

    def close(self) -> None:
        pass


def import_from(fake: FakeSpend, conn, now: datetime):  # type: ignore[no-untyped-def]
    """Run the real import pass against the fake."""
    return import_once(conn, fake, now)
