"""
cdr_market_data.py
==================

Pobiera dzienne OHLCV dla CD Projekt (CDR.WA) oraz benchmarki rynkowe
(WIG, WIG-GRY / WIG.GAMES5) w oknie 2023-01-01..2024-12-31.

Strategia źródeł (z fallbackiem):
  1) CDR.WA z yfinance - sprawdzony i stabilny dla GPW.
  2) WIG, WIGs-GRY, WIG.GAMES5 - yfinance NIE ma czystych tickerów dla
     polskich indeksów branżowych, więc lecimy bezpośrednio przez
     CSV-export Stooq: https://stooq.com/q/d/?s=<sym>&d1=YYYYMMDD&d2=...&i=d
     (publicznie dostępny endpoint, brak loginu).

Wynik:
  data/cdr/prices_cdr.csv
  data/cdr/benchmark_wig.csv
  data/cdr/benchmark_wiggry.csv
  data/cdr/merged_daily.csv  - złączone na date, gotowe do analizy

Uwaga metodologiczna:
  - Dla event-study liczymy "abnormal return" jako:
        AR_t = r_CDR_t - (alpha + beta * r_benchmark_t)
    gdzie beta jest estymowane w oknie estymacyjnym (np. -120..-20
    dni przed eventem). Tu produkujemy tylko surowe zwroty - obliczenia
    AR/CAR robi się już w fazie analitycznej.
  - Wybór benchmarku: WIG to szeroki rynek, WIG-GRY to sektor. CDR
    ma duży udział w WIG-GRY - jeśli używasz WIG-GRY do AR dla CDR,
    masz ryzyko endogeniczności (CDR sam jest częścią benchmarku).
    Konserwatywnie - używaj WIG. Dla porównania pokaż obie wersje.

Zależności:
    pip install yfinance pandas requests
"""

from __future__ import annotations

import io
import math
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf


OUTPUT_DIR = Path("./data/cdr")
START = date(2023, 1, 1)
END = date(2024, 12, 31)

CDR_TICKER_YF = "CDR.WA"
STOOQ_BASE = "https://stooq.com/q/d/l/"  # CSV download endpoint

# Mapowanie nazw benchmarków -> symbole Stooq (małe litery)
BENCHMARKS_STOOQ = {
    "wig":      "wig",
    "wig_gry":  "wig_gry",   # szeroki indeks producentów gier (od 21.03.2022)
    "wig_games5": "wig.games5",  # 5 największych, koncentracja CDR ~40%
}


def fetch_cdr_yf(start: date, end: date) -> pd.DataFrame:
    """OHLCV dla CDR.WA z yfinance."""
    df = yf.download(
        CDR_TICKER_YF,
        start=start.isoformat(),
        end=end.isoformat(),
        auto_adjust=False,
        progress=False,
    )
    if df.empty:
        raise RuntimeError("yfinance zwrócił pusty DF dla CDR.WA - sprawdź łączność/ticker.")
    df = df.reset_index()
    # Spłaszcz MultiIndex (różne wersje yfinance zwracają różne struktury)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            "_".join(str(lvl) for lvl in col if lvl and lvl != CDR_TICKER_YF).lower() or col[0].lower()
            for col in df.columns
        ]
    else:
        df.columns = [c.lower() if isinstance(c, str) else str(c).lower() for c in df.columns]
    # Szukamy kolumny z datą (może się nazywać 'date', 'datetime', 'price' itp.)
    date_col = next((c for c in df.columns if "date" in c or c == "index"), None)
    if date_col and date_col != "date":
        df = df.rename(columns={date_col: "date"})
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["ticker"] = "CDR.WA"
    df["pct_return"] = df["close"].pct_change()
    df["log_return"] = np.log(df["close"] / df["close"].shift(1))
    return df[["date", "ticker", "open", "high", "low", "close",
               "volume", "pct_return", "log_return"]]


def fetch_stooq_index(symbol: str, start: date, end: date) -> pd.DataFrame:
    """
    Pobiera dzienny CSV z Stooq. Endpoint przyjmuje:
        s = symbol (np. wig, wig_gry, wig.games5)
        d1, d2 = daty w formacie YYYYMMDD
        i  = d (dzienne)
    """
    params = {
        "s": symbol,
        "d1": start.strftime("%Y%m%d"),
        "d2": end.strftime("%Y%m%d"),
        "i": "d",
    }
    r = requests.get(STOOQ_BASE, params=params,
                     headers={"User-Agent": "AGH-WZ-academic-research/0.1"},
                     timeout=30)
    r.raise_for_status()
    text = r.text.strip()
    if not text or text.lower().startswith("no data"):
        raise RuntimeError(f"Stooq nie zwrócił danych dla {symbol}")
    df = pd.read_csv(io.StringIO(text))
    # Stooq nagłówki: Date,Open,High,Low,Close,Volume
    df.columns = [c.lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["symbol"] = symbol
    df["pct_return"] = df["close"].pct_change()
    return df[["date", "symbol", "open", "high", "low", "close", "pct_return"]]


def build_merged(cdr: pd.DataFrame, benches: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Złącza CDR z benchmarkami po dacie (left join na CDR)."""
    out = cdr[["date", "close", "volume", "pct_return"]].copy()
    out = out.rename(columns={"close": "cdr_close",
                              "volume": "cdr_volume",
                              "pct_return": "cdr_ret"})
    for name, df in benches.items():
        b = df[["date", "close", "pct_return"]].rename(
            columns={"close": f"{name}_close", "pct_return": f"{name}_ret"})
        out = out.merge(b, on="date", how="left")
    # flagi pomocnicze
    out["is_trading_day"] = ~out["cdr_close"].isna()
    out = out.sort_values("date").reset_index(drop=True)
    return out


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Pobieram CDR.WA z yfinance...")
    cdr = fetch_cdr_yf(START, END)
    cdr.to_csv(OUTPUT_DIR / "prices_cdr.csv", index=False)
    print(f"  -> {len(cdr)} sesji, zapisano prices_cdr.csv")

    benches = {}
    for name, sym in BENCHMARKS_STOOQ.items():
        try:
            print(f"Pobieram {name} ({sym}) ze Stooq...")
            b = fetch_stooq_index(sym, START, END)
            b.to_csv(OUTPUT_DIR / f"benchmark_{name}.csv", index=False)
            benches[name] = b
            print(f"  -> {len(b)} sesji, zapisano benchmark_{name}.csv")
        except Exception as e:
            print(f"  ! Pominięto {name}: {e}")

    merged = build_merged(cdr, benches)
    merged.to_csv(OUTPUT_DIR / "merged_daily.csv", index=False)
    print(f"\nZłączony dataset: {len(merged)} wierszy -> merged_daily.csv")
    print(merged.head())


if __name__ == "__main__":
    main()
