"""Blood Market — real-time stock/crypto/commodity trading with BHC coins."""

import io
import logging
import asyncio
from functools import lru_cache
from datetime import datetime, timezone

import yfinance as yf
import matplotlib
matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

log = logging.getLogger("blood.market")

# ── Popular tickers ──────────────────────────────────────────────────────────

COIN = "<:bhc_coin:1487721099897606286>"

POPULAR = {
    # Stocks
    "NVDA":  "NVIDIA",
    "AAPL":  "Apple",
    "TSLA":  "Tesla",
    "MSFT":  "Microsoft",
    "GOOG":  "Google",
    "AMZN":  "Amazon",
    "META":  "Meta",
    "AMD":   "AMD",
    # Crypto
    "BTC-USD":  "Bitcoin",
    "ETH-USD":  "Ethereum",
    "SOL-USD":  "Solana",
    "DOGE-USD": "Dogecoin",
    # Commodities
    "GC=F":  "Gold",
    "SI=F":  "Silver",
    "CL=F":  "Oil (WTI)",
}

# Aliases so users can type "gold" instead of "GC=F"
ALIASES = {
    "GOLD": "GC=F", "SILVER": "SI=F", "OIL": "CL=F",
    "BTC": "BTC-USD", "BITCOIN": "BTC-USD",
    "ETH": "ETH-USD", "ETHEREUM": "ETH-USD",
    "SOL": "SOL-USD", "SOLANA": "SOL-USD",
    "DOGE": "DOGE-USD", "DOGECOIN": "DOGE-USD",
    "NVIDIA": "NVDA", "APPLE": "AAPL", "TESLA": "TSLA",
    "MICROSOFT": "MSFT", "GOOGLE": "GOOG", "AMAZON": "AMZN",
}


def resolve_ticker(raw: str) -> str:
    """Resolve user input to a Yahoo Finance ticker."""
    t = raw.upper().strip()
    return ALIASES.get(t, t)


def get_display_name(ticker: str) -> str:
    if ticker in POPULAR:
        return f"{POPULAR[ticker]} ({ticker})"
    return ticker


async def get_price(ticker: str) -> dict | None:
    """Fetch current price info. Returns dict or None on failure."""
    def _fetch():
        try:
            t = yf.Ticker(ticker)
            info = t.fast_info
            price = info.get("lastPrice") or info.get("last_price")
            prev = info.get("previousClose") or info.get("previous_close")
            if price is None:
                return None
            change = ((price - prev) / prev * 100) if prev else 0
            return {
                "ticker": ticker,
                "price": round(price, 2),
                "prev_close": round(prev, 2) if prev else None,
                "change_pct": round(change, 2),
                "currency": info.get("currency", "USD"),
            }
        except Exception as e:
            log.warning("Failed to fetch %s: %s", ticker, e)
            return None
    return await asyncio.get_event_loop().run_in_executor(None, _fetch)


async def get_chart(ticker: str, period: str = "5d") -> io.BytesIO | None:
    """Generate a price chart as PNG bytes."""
    def _gen():
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period=period)
            if hist.empty or len(hist) < 2:
                return None

            fig, ax = plt.subplots(figsize=(8, 3.5), facecolor="#1a1a2e")
            ax.set_facecolor("#1a1a2e")

            prices = hist["Close"]
            dates = hist.index

            # Color: green if up, red if down
            color = "#00ff88" if prices.iloc[-1] >= prices.iloc[0] else "#ff4444"
            ax.plot(dates, prices, color=color, linewidth=2)
            ax.fill_between(dates, prices, alpha=0.15, color=color)

            # Style
            ax.tick_params(colors="#888888", labelsize=8)
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.spines["left"].set_color("#333333")
            ax.spines["bottom"].set_color("#333333")
            ax.yaxis.label.set_color("#888888")
            ax.grid(axis="y", color="#222244", linewidth=0.5)

            # Title
            name = POPULAR.get(ticker, ticker)
            current = prices.iloc[-1]
            change = ((current - prices.iloc[0]) / prices.iloc[0]) * 100
            sign = "+" if change >= 0 else ""
            ax.set_title(
                f"{name}  {current:,.2f} BHC  ({sign}{change:.1f}%)",
                color="white", fontsize=12, fontweight="bold", pad=10
            )

            ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
            fig.autofmt_xdate()
            plt.tight_layout()

            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
            plt.close(fig)
            buf.seek(0)
            return buf
        except Exception as e:
            log.warning("Chart gen failed for %s: %s", ticker, e)
            return None
    return await asyncio.get_event_loop().run_in_executor(None, _gen)


async def get_market_overview() -> list[dict]:
    """Fetch prices for all popular tickers."""
    results = []
    tickers = list(POPULAR.keys())

    async def _fetch_one(t):
        data = await get_price(t)
        if data:
            results.append(data)

    await asyncio.gather(*[_fetch_one(t) for t in tickers])
    # Sort by original order
    order = {t: i for i, t in enumerate(tickers)}
    results.sort(key=lambda x: order.get(x["ticker"], 99))
    return results
