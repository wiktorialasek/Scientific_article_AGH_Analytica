"""
cdr_daily_aggregate.py
======================

Agregacja dzienna postów + cen CDR + benchmarków + flag eventowych.

Heurystyki (uzasadnienie w METHODOLOGY_NOTES.md):
- Reguła dnia handlowego GPW: sesja kończy się o 17:05 czasu warszawskiego.
  Wpisy >= 17:05 oraz weekendowe są przypisywane do NASTĘPNEJ sesji.
  Wynik: kolumna `trading_day`, która łączy posty z cenami.
- Deduplikacja: jeśli ten sam content_hash + author_hmac powtarza się
  w oknie 24h, zostaje pierwsze wystąpienie (chroni przed cross-postingiem).
- Per-day metryki:
    n_posts, n_unique_authors, share_replies, n_urls_sum, n_emojis_sum,
    median_len, mean_len, sentiment_* (placeholdery - dopina się po
    etykietowaniu w osobnym skrypcie),
- Per-day rynek:
    cdr_close, cdr_ret, wig_ret, excess_ret_vs_wig,
    rolling_vol_5d, rolling_vol_20d,
- Eventy:
    flag_report_day, flag_report_window_5d, flag_game_release,
    flag_game_release_window_5d.

Wejście:
    data/cdr/posts_raw.csv           (z cdr_forum_scraper.py)
    data/cdr/merged_daily.csv        (z cdr_market_data.py)
    events_cdr.csv                   (dostarczony - patrz METHODOLOGY)

Wyjście:
    data/cdr/daily_panel.csv         - jeden wiersz na dzień handlowy

Zależności:
    pip install pandas
"""

from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo  # Python 3.9+

import pandas as pd

WARSAW = ZoneInfo("Europe/Warsaw")
GPW_SESSION_END = time(17, 5)  # 17:05 czasu warszawskiego


def assign_trading_day(ts_utc: pd.Timestamp,
                       trading_days: set) -> pd.Timestamp | None:
    """
    Mapuje timestamp posta do dnia handlowego GPW.

    Reguła:
      - jeśli post jest w dzień handlowy PRZED 17:05 warszawskiego -> ten dzień,
      - jeśli post jest >= 17:05 lub w weekend/święto -> najbliższy następny
        dzień handlowy.
    """
    if pd.isna(ts_utc):
        return None
    # konwersja do Warsaw
    ts_warsaw = ts_utc.tz_convert(WARSAW)
    candidate = ts_warsaw.date()
    # >= 17:05 -> przesuń o 1 dzień
    if ts_warsaw.time() >= GPW_SESSION_END:
        candidate = candidate + timedelta(days=1)
    # przewiń do najbliższego dnia handlowego
    for _ in range(10):  # max 10 dni w przód - bezpiecznik dla długich weekendów
        if candidate in trading_days:
            return pd.Timestamp(candidate)
        candidate = candidate + timedelta(days=1)
    return None  # poza zakresem danych rynkowych


def deduplicate_posts(df: pd.DataFrame) -> pd.DataFrame:
    """
    Usuwa duplikaty content_hash+author_hmac w oknie 24h.
    Zachowuje pierwsze wystąpienie.
    """
    df = df.sort_values("timestamp_utc").copy()
    df["timestamp_dt"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    # klucz duplikatu
    df["_dup_key"] = df["content_hash"].astype(str) + "|" + df["author_hmac"].astype(str)
    # zachowaj pierwsze wystąpienie w obrębie klucza w oknie 24h
    df["_first_seen"] = df.groupby("_dup_key")["timestamp_dt"].transform("min")
    df["_age_h"] = (df["timestamp_dt"] - df["_first_seen"]).dt.total_seconds() / 3600
    # zostaw tylko first_seen albo > 24h od first_seen
    mask = (df["_age_h"] == 0) | (df["_age_h"] > 24)
    out = df[mask].drop(columns=["_dup_key", "_first_seen", "_age_h"])
    return out


def aggregate_daily(posts: pd.DataFrame, trading_days: set) -> pd.DataFrame:
    """Agregacja per trading_day."""
    posts = posts.copy()
    posts["timestamp_dt"] = pd.to_datetime(posts["timestamp_utc"], utc=True)
    posts["trading_day"] = posts["timestamp_dt"].apply(
        lambda ts: assign_trading_day(ts, trading_days)
    )
    posts = posts.dropna(subset=["trading_day"])
    posts["trading_day"] = pd.to_datetime(posts["trading_day"]).dt.date

    g = posts.groupby("trading_day")
    agg = g.agg(
        n_posts=("content_hash", "count"),
        n_unique_authors=("author_hmac", "nunique"),
        n_guest_posts=("author_was_guest", "sum"),
        n_replies=("is_reply", "sum"),
        n_with_quote=("had_quote_block", "sum"),
        n_urls_sum=("n_urls", "sum"),
        n_emojis_sum=("n_emojis", "sum"),
        median_len=("content_len_chars", "median"),
        mean_len=("content_len_chars", "mean"),
        max_len=("content_len_chars", "max"),
    ).reset_index()

    agg["share_replies"] = agg["n_replies"] / agg["n_posts"]
    agg["share_guest"] = agg["n_guest_posts"] / agg["n_posts"]
    agg["posts_per_author"] = agg["n_posts"] / agg["n_unique_authors"]

    # Placeholdery sentymentu - wypełnione po etykietowaniu w osobnym skrypcie
    agg["mean_sentiment"] = pd.NA
    agg["median_sentiment"] = pd.NA
    agg["share_positive"] = pd.NA
    agg["share_negative"] = pd.NA

    return agg


def add_market_data(daily: pd.DataFrame, merged_daily: pd.DataFrame) -> pd.DataFrame:
    """
    Dołącza dane rynkowe i liczy excess return + rolling volatility.
    """
    md = merged_daily.copy()
    md["date"] = pd.to_datetime(md["date"]).dt.date
    md = md.rename(columns={"cdr_ret": "cdr_ret", "wig_ret": "wig_ret"})

    # excess return vs WIG
    if "wig_ret" in md.columns:
        md["excess_ret_vs_wig"] = md["cdr_ret"] - md["wig_ret"]
    else:
        md["excess_ret_vs_wig"] = pd.NA

    # rolling volatility (annualized) - 5d i 20d
    md = md.sort_values("date")
    md["rolling_vol_5d"] = md["cdr_ret"].rolling(5, min_periods=3).std() * (252 ** 0.5)
    md["rolling_vol_20d"] = md["cdr_ret"].rolling(20, min_periods=10).std() * (252 ** 0.5)

    out = daily.merge(md, left_on="trading_day", right_on="date", how="outer")
    # Dla dni z md bez postów: trading_day=NaN, date=valid — koalescuj
    if "date" in out.columns:
        mask_no_td = out["trading_day"].isna()
        out.loc[mask_no_td, "trading_day"] = out.loc[mask_no_td, "date"]
        out = out.drop(columns=["date"])
    out["trading_day"] = pd.to_datetime(out["trading_day"]).dt.date
    return out.sort_values("trading_day").reset_index(drop=True)


def add_event_flags(daily: pd.DataFrame, events_csv: Path,
                    window_days: int = 5) -> pd.DataFrame:
    """
    Dokleja flagi eventowe na podstawie events_cdr.csv.
    Dla każdego dnia: czy jest dniem raportu / premiery / w oknie +/- N dni.
    """
    if not events_csv.exists():
        print(f"WARN: {events_csv} nie istnieje - pomijam flagi eventów.")
        return daily

    events = pd.read_csv(events_csv, parse_dates=["date"])
    events["date"] = events["date"].dt.date

    daily = daily.copy()
    daily["trading_day"] = pd.to_datetime(daily["trading_day"]).dt.date

    daily["flag_report_day"] = 0
    daily["flag_report_window_5d"] = 0
    daily["flag_game_event"] = 0
    daily["flag_game_event_window_5d"] = 0
    daily["event_label"] = ""

    for _, ev in events.iterrows():
        ev_date = ev["date"]
        ev_type = ev["event_type"]  # "report" | "game" | "other"
        ev_label = ev["label"]
        # exact match
        mask_exact = daily["trading_day"] == ev_date
        # +/- window_days - konwertuj date->date żeby porównanie działało
        lo = ev_date - timedelta(days=window_days)
        hi = ev_date + timedelta(days=window_days)
        mask_window = (daily["trading_day"] >= lo) & (daily["trading_day"] <= hi)  # type: ignore[operator]

        if ev_type == "report":
            daily.loc[mask_exact, "flag_report_day"] = 1
            daily.loc[mask_window, "flag_report_window_5d"] = 1
        elif ev_type == "game":
            daily.loc[mask_exact, "flag_game_event"] = 1
            daily.loc[mask_window, "flag_game_event_window_5d"] = 1

        daily.loc[mask_exact, "event_label"] = (
            daily.loc[mask_exact, "event_label"].astype(str) + f"|{ev_label}"
        ).str.strip("|")

    return daily


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--posts", default="data/cdr/posts_raw.csv")
    ap.add_argument("--merged", default="data/cdr/merged_daily.csv")
    ap.add_argument("--events", default="events_cdr.csv")
    ap.add_argument("--output", default="data/cdr/daily_panel.csv")
    args = ap.parse_args()

    print(f"Wczytuję posty: {args.posts}")
    posts = pd.read_csv(args.posts)
    print(f"  -> {len(posts)} postów surowo")

    posts = deduplicate_posts(posts)
    print(f"  -> {len(posts)} po deduplikacji")

    print(f"Wczytuję dane rynkowe: {args.merged}")
    merged = pd.read_csv(args.merged)
    trading_days = set(pd.to_datetime(merged["date"]).dt.date)
    print(f"  -> {len(trading_days)} dni handlowych")

    print("Agreguję per trading_day (reguła 17:05 + weekend->next session)...")
    daily = aggregate_daily(posts, trading_days)
    print(f"  -> {len(daily)} dni z postami")

    print("Dołączam dane rynkowe i excess return / rolling vol...")
    panel = add_market_data(daily, merged)

    print(f"Dodaję flagi eventowe z {args.events}...")
    panel = add_event_flags(panel, Path(args.events))

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    panel.to_csv(out_path, index=False)
    print(f"\nGotowe -> {out_path}")
    print(f"Kolumny: {list(panel.columns)}")
    print(f"\nPodgląd:")
    print(panel.head(10))


if __name__ == "__main__":
    main()
