"""Supported instruments. Adding a symbol is one entry here."""

from __future__ import annotations

from dataclasses import dataclass


class UnknownSymbolError(ValueError):
    pass


@dataclass(frozen=True)
class Instrument:
    symbol: str          # continuous-contract ticker as TradingView shows it, e.g. "GC1!"
    exchange: str        # TradingView exchange prefix
    folder: str          # filesystem-safe name
    description: str
    code: str            # short root used in captions / detector calls
    point_value: float   # account currency per 1.00 point of price, one contract

    @property
    def tv_symbol(self) -> str:
        return f"{self.exchange}:{self.symbol}"


REGISTRY: dict[str, Instrument] = {
    "GC1!": Instrument(
        "GC1!", "COMEX", "gc1", "Gold futures (COMEX), continuous front month", "GC",
        point_value=100.0),
}

_ALIASES = {"GC": "GC1!", "GC1": "GC1!", "COMEX:GC1!": "GC1!", "GOLD": "GC1!"}


def resolve(symbol: str) -> Instrument:
    key = symbol.strip().upper()
    key = _ALIASES.get(key, key)
    try:
        return REGISTRY[key]
    except KeyError:
        supported = ", ".join(REGISTRY)
        raise UnknownSymbolError(
            f"Unsupported symbol {symbol!r}. Supported: {supported}."
        ) from None
