"""
cdr_forum_scraper.py
====================

Scraper forum Bankier.pl dla CD Projekt SA (CDR) - wersja do pracy naukowej.

WERSJA 0.2 - zmiany względem 0.1 (po konsultacji metodologicznej):
- HMAC-SHA256 zamiast SHA-256+sól (lepsza pseudonimizacja pod RODO),
  klucz HMAC trzymany w .anon_hmac_key OSOBNO od repo,
- content_hash w każdym wierszu - do deduplikacji i audytu,
- parser_version w każdym wierszu - by móc reidentyfikować wiersze
  do reparsowania po zmianie selektorów,
- integracja z cdr_text_clean (URL-e i emoji jako osobne countery),
- has_quote_block i is_reply jako osobne flagi.

Założenia compliance:
- czyta i respektuje robots.txt PRZED każdą sesją,
- jawnie identyfikuje się w User-Agent (instytucja + cel + kontakt),
- domyślny throttle 2.5 s + jitter, eksponencjalny backoff,
- nie omija autentykacji, CAPTCHA, rate-limitera ani User-Agent banów,
- cache'uje surowe HTML - powtórne uruchomienia nie biją serwera,
- nicki -> HMAC-SHA256 z lokalnym sekretem; oryginałów nigdzie nie ma.

Podstawa prawna (do sekcji metodologicznej):
- art. 27 ustawy o prawie autorskim (dozwolony użytek dla badań),
- art. 3 dyrektywy DSM 2019/790 (wyjątek TDM dla badań naukowych w UE),
- art. 89 RODO (cele badawcze + pseudonimizacja).

Wymagane:
    pip install requests beautifulsoup4 lxml

Uruchomienie:
    1. Edytuj USER_CONTACT (e-mail uczelniany).
    2. Sprawdź regulamin https://www.bankier.pl/static/forum_regulamin
       i robots.txt - skrypt sprawdza robots automatycznie.
    3. python cdr_forum_scraper.py --start 2023-01-01 --end 2024-12-31
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import logging
import os
import random
import re
import secrets
import sys
import time
import urllib.robotparser as robotparser
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from cdr_text_clean import clean_post, content_hash_for_dedup

# =====================================================================
# KONFIGURACJA - edytuj przed pierwszym uruchomieniem
# =====================================================================

USER_CONTACT = "wiktorialasek.03@gmail.com"  # WYMAGANE: realny adres
INSTITUTION = "AGH WZ - praca naukowa o sentymencie inwestorskim"

PARSER_VERSION = "0.2.0"

BASE_URL = "https://www.bankier.pl"
FORUM_ROOT_PATH = "/forum/forum_o_cd-projekt,6,21,353.html"
ROBOTS_URL = "https://www.bankier.pl/robots.txt"

MIN_DELAY_SEC = 2.5
JITTER_SEC = 1.0
MAX_RETRIES = 3
HARD_STOP_AFTER_CONSECUTIVE_ERRORS = 5
REQUEST_TIMEOUT_SEC = 30

CACHE_DIR = Path("./cache/bankier_cdr")
OUTPUT_DIR = Path("./data/cdr")
LOG_DIR = Path("./logs")
HMAC_KEY_FILE = Path("./.anon_hmac_key")  # NIE commituj; chmod 600

USER_AGENT = (
    f"AGH-WZ-Research-Bot/{PARSER_VERSION} "
    f"(+contact: {USER_CONTACT}; purpose: academic-sentiment-research; "
    f"institution: {INSTITUTION}; respects robots.txt; rate-limited)"
)


@dataclass
class Post:
    thread_id: str
    thread_title: str
    post_idx_in_thread: int
    page_idx_in_thread: int
    timestamp_utc: str
    author_hmac: str
    author_was_guest: bool
    content_clean: str
    content_hash: str
    content_len_chars: int
    content_len_chars_raw: int
    is_reply: bool
    had_quote_block: bool
    n_urls: int
    n_emojis: int
    parent_post_idx: int | None
    votes_up: int
    votes_down: int
    url_canonical: str
    parser_version: str
    scraped_at_utc: str


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("cdr_scraper")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(LOG_DIR / "scraper.log", encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def load_or_create_hmac_key() -> bytes:
    """Klucz HMAC do pseudonimizacji nicków. NIGDY nie commituj."""
    if HMAC_KEY_FILE.exists():
        return HMAC_KEY_FILE.read_bytes()
    key = secrets.token_bytes(32)
    HMAC_KEY_FILE.write_bytes(key)
    try:
        os.chmod(HMAC_KEY_FILE, 0o600)
    except OSError:
        print(f"WARN: chmod 600 nie udał się na {HMAC_KEY_FILE}. "
              "Sprawdź ręcznie, że plik jest poza repo.", file=sys.stderr)
    return key


def author_hmac(nick: str, key: bytes) -> str:
    h = hmac.new(key, nick.encode("utf-8"), hashlib.sha256)
    return h.hexdigest()[:20]


def verify_robots(robots_url: str, target_paths: list[str],
                  logger: logging.Logger) -> robotparser.RobotFileParser:
    rp = robotparser.RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
    except Exception as e:
        logger.error(f"Nie udało się odczytać {robots_url}: {e}. STOP.")
        raise SystemExit(1)
    logger.info(f"robots.txt z {robots_url} - odczytane.")
    for path in target_paths:
        full = urljoin(BASE_URL, path)
        allowed = rp.can_fetch(USER_AGENT, full)
        logger.info(f"robots.txt allow={allowed} dla {full}")
        if not allowed:
            logger.error(f"robots.txt zabrania crawlu {full}.")
            raise SystemExit(2)
    cd = rp.crawl_delay(USER_AGENT) or rp.crawl_delay("*")
    if cd:
        logger.info(f"robots.txt sugeruje crawl-delay={cd}s")
    return rp


class PoliteSession:
    def __init__(self, logger: logging.Logger, robots: robotparser.RobotFileParser):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.5",
        })
        self.logger = logger
        self.robots = robots
        self.consecutive_errors = 0
        self.last_request_ts = 0.0
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _wait(self):
        elapsed = time.time() - self.last_request_ts
        delay = MIN_DELAY_SEC + random.uniform(0, JITTER_SEC)
        cd = self.robots.crawl_delay(USER_AGENT) or self.robots.crawl_delay("*")
        if cd:
            delay = max(delay, float(cd))
        if elapsed < delay:
            time.sleep(delay - elapsed)

    def _cache_path(self, url: str) -> Path:
        h = hashlib.sha1(url.encode()).hexdigest()[:24]
        return CACHE_DIR / f"{h}.html"

    def get(self, url: str, use_cache: bool = True) -> str:
        if not self.robots.can_fetch(USER_AGENT, url):
            raise RuntimeError(f"robots.txt zabrania {url}")

        cached = self._cache_path(url)
        if use_cache and cached.exists():
            return cached.read_text("utf-8")

        self._wait()
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self.logger.info(f"GET {url} (attempt {attempt})")
                resp = self.s.get(url, timeout=REQUEST_TIMEOUT_SEC)
                self.last_request_ts = time.time()
                if resp.status_code == 200:
                    self.consecutive_errors = 0
                    cached.write_text(resp.text, "utf-8")
                    return resp.text
                if resp.status_code in (403, 429):
                    self.logger.error(f"HTTP {resp.status_code} z {url}.")
                    self.consecutive_errors += 1
                    if self.consecutive_errors >= HARD_STOP_AFTER_CONSECUTIVE_ERRORS:
                        raise RuntimeError("Hard stop: zbyt wiele odmów.")
                    time.sleep(60 * attempt)
                    continue
                if 500 <= resp.status_code < 600:
                    time.sleep(5 * attempt)
                    continue
                self.logger.error(f"HTTP {resp.status_code}: {url}")
                self.consecutive_errors += 1
                return ""
            except requests.RequestException as e:
                self.logger.warning(f"Wyjątek {url}: {e}")
                time.sleep(5 * attempt)
        self.consecutive_errors += 1
        if self.consecutive_errors >= HARD_STOP_AFTER_CONSECUTIVE_ERRORS:
            raise RuntimeError("Hard stop: zbyt wiele błędów.")
        return ""


PL_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
    "października": 10, "listopada": 11, "grudnia": 12,
}

TS_PATTERNS = [
    re.compile(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})"),
    re.compile(r"(\d{1,2})\s+(stycznia|lutego|marca|kwietnia|maja|czerwca|"
               r"lipca|sierpnia|września|października|listopada|grudnia)"
               r"\s+(\d{4}),?\s+(\d{2}):(\d{2})"),
]


def parse_timestamp_pl(text: str) -> str | None:
    """Bankier publikuje czas Europe/Warsaw. Zwracamy ISO bez konwersji TZ -
    konwersja na strefę GPW jest w cdr_daily_aggregate.py."""
    if not text:
        return None
    m = TS_PATTERNS[0].search(text)
    if m:
        y, mo, d, hh, mm = map(int, m.groups())
        try:
            return datetime(y, mo, d, hh, mm, tzinfo=timezone.utc).isoformat()
        except ValueError:
            return None
    m = TS_PATTERNS[1].search(text)
    if m:
        d_, mon_pl, y_, hh, mm = m.groups()
        try:
            return datetime(int(y_), PL_MONTHS[mon_pl], int(d_),
                            int(hh), int(mm), tzinfo=timezone.utc).isoformat()
        except (ValueError, KeyError):
            return None
    return None


def parse_thread_links_from_forum_page(html: str, page_url: str = "") -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    # Bankier używa relatywnych linków: "temat_...,ID.html" (bez prefiksu /forum/)
    base = page_url or urljoin(BASE_URL, FORUM_ROOT_PATH)
    out = []
    for a in soup.select("a[href*='temat_'], a[href*='/forum/pokaz-tresc']"):
        href = a.get("href", "")
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 5:
            continue
        out.append((urljoin(base, href), title))
    seen, uniq = set(), []
    for url, t in out:
        if url in seen:
            continue
        seen.add(url)
        uniq.append((url, t))
    return uniq


def extract_thread_id(url: str) -> str:
    m = re.search(r"(?:temat_[\w-]+,|thread_id=)(\d+)", url)
    return m.group(1) if m else hashlib.sha1(url.encode()).hexdigest()[:12]


def parse_single_post_page(
    html: str, thread_id: str, thread_title: str,
    post_idx: int, hmac_key: bytes, canonical_url: str,
) -> tuple["Post | None", "str | None"]:
    """
    Bankier: każdy post to osobna strona. Wyciąga post z #boxThread
    i zwraca (Post|None, next_url|None).
    """
    soup = BeautifulSoup(html, "lxml")
    box = soup.select_one("#boxThread")
    if not box:
        return None, None

    # Autor
    author_span = box.select_one("span.author.name")
    if not author_span:
        return None, None
    nick_raw = re.sub(r"Autor:\s*", "", author_span.get_text(strip=True))
    was_guest = nick_raw.startswith("~")
    nick_clean = nick_raw.lstrip("~")
    ahmac = author_hmac(nick_clean, hmac_key)

    # Timestamp z atrybutu datetime (format: "YYYY-MM-DD HH:MM")
    time_el = box.select_one("time.entry-date")
    ts = parse_timestamp_pl(time_el.get("datetime", "").strip()) if time_el else None
    if not ts:
        return None, None

    # Treść posta
    body_el = box.select_one("div.boxContent div.p") or box.select_one("div.boxContent")
    body_raw = body_el.get_text("\n", strip=True) if body_el else ""

    cleaned = clean_post(body_raw, title=thread_title)
    if not cleaned["text_clean"] or cleaned["n_chars_clean"] < 5:
        # post bez treści - ale sprawdź czy jest następna strona
        next_el = box.select_one("a.next_post")
        next_url = urljoin(canonical_url, next_el["href"]) if next_el and next_el.get("href") else None
        return None, next_url

    is_rep = thread_title.lower().startswith("re:") or cleaned["is_reply"]

    # Głosy: <a class="addCommentUp up"><span class="voteValue">N</span></a>
    def _vote(css: str) -> int:
        el = box.select_one(css)
        if el:
            v = el.select_one(".voteValue")
            try:
                return int(v.get_text(strip=True)) if v else 0
            except (ValueError, AttributeError):
                return 0
        return 0

    votes_up = _vote("a.addCommentUp")
    votes_down = _vote("a.addCommentDown")

    post = Post(
        thread_id=thread_id,
        thread_title=thread_title,
        post_idx_in_thread=post_idx,
        page_idx_in_thread=post_idx,
        timestamp_utc=ts,
        author_hmac=ahmac,
        author_was_guest=was_guest,
        content_clean=cleaned["text_clean"][:10000],
        content_hash=content_hash_for_dedup(cleaned["text_clean"]),
        content_len_chars=cleaned["n_chars_clean"],
        content_len_chars_raw=cleaned["n_chars_original"],
        is_reply=is_rep,
        had_quote_block=cleaned["had_quote_block"],
        n_urls=cleaned["n_urls"],
        n_emojis=cleaned["n_emojis"],
        parent_post_idx=post_idx - 1 if is_rep and post_idx > 0 else None,
        votes_up=votes_up,
        votes_down=votes_down,
        url_canonical=canonical_url,
        parser_version=PARSER_VERSION,
        scraped_at_utc=datetime.now(timezone.utc).isoformat(),
    )

    next_el = box.select_one("a.next_post")
    next_url = urljoin(canonical_url, next_el["href"]) if next_el and next_el.get("href") else None
    return post, next_url


def iterate_forum_pages(session: PoliteSession, start_url: str,
                        max_pages: int, logger: logging.Logger,
                        start_page: int = 1) -> Iterator[tuple[str, str]]:
    page = start_page
    seen_threads: set[str] = set()
    base_no_ext = re.sub(r"\.html$", "", start_url)
    current_url = start_url if page == 1 else f"{base_no_ext},{page}.html"
    while page <= start_page + max_pages - 1:
        logger.info(f"Lista wątków, strona {page}: {current_url}")
        html = session.get(current_url)
        if not html:
            break
        threads = parse_thread_links_from_forum_page(html, current_url)
        new_count = 0
        for url, title in threads:
            if url in seen_threads:
                continue
            seen_threads.add(url)
            new_count += 1
            yield url, title
        if new_count == 0:
            logger.info("Brak nowych wątków - koniec paginacji.")
            break
        page += 1
        current_url = f"{base_no_ext},{page}.html"


def scrape_thread(session: PoliteSession, thread_url: str, thread_title: str,
                  hmac_key: bytes, max_pages: int, logger: logging.Logger,
                  date_min, date_max) -> list[Post]:
    """
    Bankier: pierwszy post to strona wątku, kolejne posty są pod linkami
    a.next_post. Podążamy tym łańcuchem aż do wyczerpania lub limitu.
    """
    thread_id = extract_thread_id(thread_url)
    all_posts: list[Post] = []
    current_url: str | None = thread_url
    post_idx = 0
    seen_urls: set[str] = set()

    while current_url and post_idx < max_pages:
        if current_url in seen_urls:
            break
        seen_urls.add(current_url)

        html = session.get(current_url)
        if not html:
            break

        post, next_url = parse_single_post_page(
            html, thread_id, thread_title, post_idx, hmac_key, current_url
        )

        if post:
            try:
                pdate = datetime.fromisoformat(post.timestamp_utc).date()
            except ValueError:
                pdate = None

            if pdate:
                if pdate > date_max:
                    break  # posty są chronologiczne - dalej będzie jeszcze nowiej
                if pdate >= date_min:
                    all_posts.append(post)

        current_url = next_url
        post_idx += 1

    logger.info(f"Wątek {thread_id} ({thread_title[:60]}): {len(all_posts)} postów")
    return all_posts


def write_posts_csv(posts, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(Post.__dataclass_fields__.keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for p in posts:
            w.writerow(asdict(p))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--max-forum-pages", type=int, default=200)
    ap.add_argument("--max-thread-pages", type=int, default=80)
    ap.add_argument("--limit-threads", type=int, default=None)
    ap.add_argument("--start-page", type=int, default=1,
                    help="Numer strony listy forum od której zacząć (np. 1790)")
    args = ap.parse_args()

    if USER_CONTACT.startswith("TWOJ_EMAIL"):
        print("ERROR: Edytuj USER_CONTACT przed uruchomieniem.", file=sys.stderr)
        sys.exit(3)

    date_min = datetime.fromisoformat(args.start).date()
    date_max = datetime.fromisoformat(args.end).date()

    logger = setup_logging()
    logger.info(f"START: window {date_min}..{date_max}, parser={PARSER_VERSION}")
    logger.info(f"UA={USER_AGENT}")

    hmac_key = load_or_create_hmac_key()
    robots = verify_robots(ROBOTS_URL, [FORUM_ROOT_PATH], logger)
    session = PoliteSession(logger, robots)

    forum_url = urljoin(BASE_URL, FORUM_ROOT_PATH)
    all_posts: list[Post] = []
    thread_index: list[dict] = []

    for i, (turl, title) in enumerate(iterate_forum_pages(
            session, forum_url, args.max_forum_pages, logger, args.start_page)):
        if args.limit_threads and i >= args.limit_threads:
            break
        posts = scrape_thread(session, turl, title, hmac_key,
                              args.max_thread_pages, logger,
                              date_min, date_max)
        all_posts.extend(posts)
        thread_index.append({
            "thread_id": extract_thread_id(turl),
            "title": title,
            "url": turl,
            "n_posts_collected": len(posts),
        })
        if (i + 1) % 25 == 0:
            write_posts_csv(all_posts, OUTPUT_DIR / "posts_raw.csv")

    write_posts_csv(all_posts, OUTPUT_DIR / "posts_raw.csv")
    with (OUTPUT_DIR / "threads_index.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["thread_id", "title", "url", "n_posts_collected"])
        w.writeheader()
        w.writerows(thread_index)

    logger.info(f"DONE. {len(all_posts)} postów z {len(thread_index)} wątków.")


if __name__ == "__main__":
    main()
