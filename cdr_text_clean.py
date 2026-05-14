"""
cdr_text_clean.py
=================

Moduł czyszczenia tekstu postów forum przed analizą sentymentu.

Heurystyki przyjęte (uzasadnienie w METHODOLOGY_NOTES.md):
- usuń bloki cytatów rodzica - wpływałyby na sentyment, mimo że nie są
  wypowiedzią autora,
- wyciągnij URL-e i emoji jako OSOBNE liczniki (feature), nie jako tekst,
  bo HerBERT/FinBERT i lokalne LLM-y nie obsługują ich konsekwentnie,
- znormalizuj whitespace, zostaw diakrytyki (PL),
- zachowaj minimalną długość 5 znaków - krótsze posty to z reguły śmiecie
  ("xD", "+1", ".") lub nawigacja serwisu.

Wynik: oczyszczony tekst + dict z metrykami:
    {
      "text_clean": str,
      "n_urls": int,
      "n_emojis": int,
      "n_chars_original": int,
      "n_chars_clean": int,
      "had_quote_block": bool,
      "is_reply": bool,
    }
"""

from __future__ import annotations

import re
import unicodedata

# Regex emoji - pokrywa większość emoji unicode i emotki tekstowe
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U00002500-\U00002BEF"  # chinese char
    "\U00002702-\U000027B0"
    "\U0001f926-\U0001f937"
    "\U00010000-\U0010ffff"
    "\u2640-\u2642"
    "\u2600-\u2B55"
    "\u200d"
    "\u23cf"
    "\u23e9"
    "\u231a"
    "\ufe0f"
    "\u3030"
    "]+",
    flags=re.UNICODE,
)

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

# Linie cytatu - na Bankier często zaczynają się od ">" lub od
# "Cytat z postu użytkownika XYZ:" / "Re:"
_QUOTE_LINE_RE = re.compile(r"^\s*(>+|cytat\s+z\s+postu|cytuj[ę:])", re.IGNORECASE)

# Stopki forum / nawigacja, które przeciekają z parsowania
_FORUM_NOISE_PATTERNS = [
    re.compile(r"Zgłoś naruszenie.*", re.IGNORECASE | re.DOTALL),
    re.compile(r"Treści na Forum Bankier\.pl.*", re.IGNORECASE | re.DOTALL),
    re.compile(r"\(wiadomość\s+usunięta\s+przez\s+moderatora\)", re.IGNORECASE),
    re.compile(r"Re:\s*", re.IGNORECASE),  # tylko prefix "Re: " na początku
]

_WHITESPACE_RE = re.compile(r"\s+")


def is_reply(text: str, title: str | None = None) -> bool:
    """Wpis jest odpowiedzią, jeśli zaczyna się od 'Re:' albo zawiera cytat."""
    if title and title.strip().lower().startswith("re:"):
        return True
    if text.strip().lower().startswith("re:"):
        return True
    for line in text.splitlines()[:3]:
        if _QUOTE_LINE_RE.match(line):
            return True
    return False


def strip_quote_blocks(text: str) -> tuple[str, bool]:
    """
    Usuwa linie zaczynające się od '>' (cytat rodzica) i bloki
    "Cytat z postu...". Zwraca (tekst, had_quote).
    """
    lines = text.splitlines()
    kept = []
    had_quote = False
    in_quote_block = False
    for line in lines:
        if _QUOTE_LINE_RE.match(line):
            had_quote = True
            in_quote_block = True
            continue
        if in_quote_block:
            # pusta linia kończy blok cytatu
            if not line.strip():
                in_quote_block = False
                continue
            # heurystyka: jeśli linia jest wcięta lub krótka,
            # to nadal część cytatu
            if line.startswith((" ", "\t")) or len(line.strip()) < 40:
                continue
            in_quote_block = False
        kept.append(line)
    return "\n".join(kept), had_quote


def clean_post(raw: str, title: str | None = None) -> dict:
    """
    Główna funkcja - oczyszcza tekst i wyciąga metryki.
    """
    if not raw:
        return {
            "text_clean": "",
            "n_urls": 0,
            "n_emojis": 0,
            "n_chars_original": 0,
            "n_chars_clean": 0,
            "had_quote_block": False,
            "is_reply": False,
        }

    n_chars_original = len(raw)
    is_rep = is_reply(raw, title)

    # 1. usuń bloki cytatów
    text, had_quote = strip_quote_blocks(raw)

    # 2. policz i usuń URL-e
    urls = _URL_RE.findall(text)
    n_urls = len(urls)
    text = _URL_RE.sub(" ", text)

    # 3. policz i usuń emoji
    emojis = _EMOJI_RE.findall(text)
    n_emojis = sum(len(e) for e in emojis)  # bo grupy mogą być dłuższe niż 1 znak
    text = _EMOJI_RE.sub(" ", text)

    # 4. usuń stopki/nawigację
    for pat in _FORUM_NOISE_PATTERNS:
        text = pat.sub(" ", text)

    # 5. normalizacja unicode (NFKC ujednolica np. różne typy spacji)
    text = unicodedata.normalize("NFKC", text)

    # 6. zwiń whitespace
    text = _WHITESPACE_RE.sub(" ", text).strip()

    return {
        "text_clean": text,
        "n_urls": n_urls,
        "n_emojis": n_emojis,
        "n_chars_original": n_chars_original,
        "n_chars_clean": len(text),
        "had_quote_block": had_quote,
        "is_reply": is_rep,
    }


def content_hash_for_dedup(text_clean: str) -> str:
    """
    Hash znormalizowanej treści do deduplikacji.
    Lowercase + bez whitespace, żeby '   Brawo!  ' i 'brawo!' były
    traktowane jako duplikaty.
    """
    import hashlib
    normalized = re.sub(r"\s+", "", text_clean.lower())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


if __name__ == "__main__":
    # Smoke test
    examples = [
        ("> ~Janusz napisał:\n> kupować, idzie 500!\n\nMoim zdaniem to bzdura "
         "patrz https://stooq.pl/q/?s=cdr 😂😂 stop loss już dawno wskoczył.",
         "Re: CDR analiza"),
        ("Brawo CDR!!! 🚀🚀🚀 do księżyca", "Brawo Cd Projekt"),
        ("Zgłoś naruszenie", None),
        ("", None),
    ]
    for raw, title in examples:
        r = clean_post(raw, title)
        print("---")
        print(f"INPUT:  {raw!r}")
        print(f"OUT:    {r['text_clean']!r}")
        print(f"META:   urls={r['n_urls']} emojis={r['n_emojis']} "
              f"quote={r['had_quote_block']} reply={r['is_reply']}")
