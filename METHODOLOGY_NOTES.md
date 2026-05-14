# Notatki metodologiczne - sentyment CDR vs kurs (2023-2024)

Dokument do audytu i sekcji metodologicznej artykułu. Wszystkie decyzje
projektowe są tu uzasadnione, żeby recenzent mógł je zweryfikować.

## 1. Zakres badania

- **Spółka:** CD Projekt SA (ticker GPW: CDR; instrument yfinance: CDR.WA).
- **Źródło sentymentu:** forum Bankier.pl (`/forum/forum_o_cd-projekt,...`).
- **Okres:** 2023-01-01..2024-12-31 (488 dni handlowych GPW).
- **Częstotliwość:** agregacja dzienna na poziomie sesji handlowej GPW.

## 2. Podstawa prawna

Pseudonimowane treści z publicznego forum przetwarzane są w oparciu o:

1. **Art. 27 ust. 1 ustawy o prawie autorskim** - dozwolony użytek dla
   instytucji naukowych i edukacyjnych w celach badawczych.
2. **Art. 3 dyrektywy DSM 2019/790** (text & data mining exception) -
   wyjątek TDM dla badań naukowych w UE, pod warunkiem legalnego dostępu
   i braku obchodzenia zabezpieczeń technicznych.
3. **Art. 89 RODO** - przetwarzanie danych osobowych do celów badań
   naukowych, z zachowaniem zasady minimalizacji i pseudonimizacji.

**Działania komplementarne, które należy wykonać przed publikacją:**
- Przegląd regulaminu Bankier.pl pod kątem klauzul o automatycznym
  pobieraniu treści; w razie wątpliwości - kontakt z Bonnier Business
  (właściciel Bankier).
- Jeśli uczelnia tego wymaga - zgłoszenie do IOD / komisji etycznej.
- Korpus surowy nie jest publikowany; udostępniany jest tylko agregat
  dzienny (`daily_panel.csv`) bez treści postów.

## 3. Heurystyki przyjęte w pipeline

### 3.1 Pseudonimizacja autorów

**Wybór:** HMAC-SHA256 z 256-bitowym sekretem trzymanym w `.anon_hmac_key`
(plik chmod 600, poza repo).

**Uzasadnienie:** SHA-256 z solą jest podatne na ataki rainbow-table
przy małej przestrzeni nicków (~10 tys. unikalnych użytkowników na
forum CDR). HMAC z sekretem wymaga znajomości klucza nawet po wycieku
soli, co jest spójne z wytycznymi RODO dla pseudonimizacji.

**Konsekwencja:** wynik to dane spseudonimizowane, nie zanonimizowane
w sensie RODO. Klucz HMAC NIE może być udostępniany razem z datasetem.

### 3.2 Reguła dnia handlowego

**Problem:** GPW kończy sesję o 17:05 czasu warszawskiego. Wpisy
publikowane po tej godzinie nie mogą wpływać na cenę dnia bieżącego,
tylko na sesję następną.

**Heurystyka:**
- jeśli post jest w dzień handlowy GPW i przed 17:05 Europe/Warsaw
  -> przypisany do TEGO dnia handlowego;
- jeśli post jest >= 17:05 lub w weekend / dzień wolny
  -> przypisany do NAJBLIŻSZEGO NASTĘPNEGO dnia handlowego.

**Implementacja:** `assign_trading_day()` w `cdr_daily_aggregate.py`.

**Konsekwencja:** redukuje look-ahead bias w testach lead/lag. Bez tej
poprawki sentyment piątkowy wieczór jest błędnie korelowany ze zwrotem
piątkowym, mimo że nie mógł na niego wpływać.

### 3.3 Deduplikacja

Klucz: `content_hash` (SHA-1 z lowercase-no-whitespace treści) +
`author_hmac`. W oknie 24h zachowywane jest pierwsze wystąpienie.

**Powód:** crosspostowanie ("kupować! kupować!" - ta sama wiadomość
w 5 wątkach w ciągu godziny przez tego samego użytkownika). Te
duplikaty zaburzyłyby wagę pojedynczego sygnału.

### 3.4 Czyszczenie tekstu

- **Bloki cytatów** (`>` linia, "Cytat z postu", wcięte) - USUWANE.
  Sentyment cytowanej treści nie należy do wypowiedzi autora.
- **URL-e** - usuwane z tekstu, ale liczone jako osobny feature `n_urls`.
  Wiele URL-i w poście to często linki do analiz - sygnał osobnego typu.
- **Emoji** - usuwane z tekstu, liczone jako `n_emojis`. HerBERT i lokalne
  LLM-y nie obsługują emoji konsekwentnie; jako feature mogą zostać
  użyte w drugiej warstwie modelu.
- **Stopki forum** ("Zgłoś naruszenie", "wiadomość usunięta przez
  moderatora") - usuwane jako noise z parsowania.
- **Minimalna długość:** 5 znaków po czyszczeniu. Krótsze są wyrzucane.

### 3.5 Benchmarki rynkowe

- **WIG** - szeroki benchmark do liczenia excess return CDR.
- **WIG-GRY** - branżowy, ale CDR ma w nim duży udział (>40% w
  WIG.GAMES5) -> ryzyko endogeniczności przy używaniu jako benchmark
  AR. Trzymany jako referencja.
- **WIG.GAMES5** - jeszcze bardziej skoncentrowany; dla CDR
  praktycznie kołowy.

**Decyzja:** główny excess_return liczony względem WIG. WIG-GRY i
WIG.GAMES5 zaciągane jako kolumny pomocnicze (`wig_gry_ret`,
`wig_games5_ret`) ale NIE używane do liczenia AR w głównej analizie.

### 3.6 Rolling volatility

`rolling_vol_5d` i `rolling_vol_20d` jako annualizowane std zwrotów.
Sentyment forum statystycznie silniej koreluje ze zmiennością niż
z kierunkiem zwrotu - dlatego oba sygnały trzeba mieć w panelu od
początku, żeby uniknąć "cherry-picking".

### 3.7 Flagi eventowe

`events_cdr.csv` zawiera:
- raporty okresowe (daty z ESPI RB 2/2023 i RB 1/2024, zweryfikowane
  na cdprojekt.com/pl/inwestorzy/raporty-gieldowe/),
- premiery gier (Cyberpunk 2.0, Phantom Liberty, patch 2.1, ogłoszenie
  Polaris, reveal Wiedźmin 4),
- flagi `_window_5d` dla event study.

**Pełna lista jest jawna i edytowalna** - jeśli recenzent kwestionuje
dobór eventów, łatwo zaktualizować bez przebudowy reszty pipeline'u.

## 4. Co świadomie odrzucono

- **WIG-INFO jako benchmark** - CDR to producent gier (GICS 5020),
  nie firma informatyczna. WIG-INFO pasuje do Asseco, Comarch.
- **KNF Rejestr Krótkiej Sprzedaży jako zmienna dzienna** - dane są
  event-based (zgłoszenie zmiany pozycji ≥0,1%), nie dzienne. Próg
  publikacji to 0,5%, więc obraz jest niekompletny.
- **Drugie źródło tekstu (Reddit, StockTwits, X)** - dla single-stock
  CDR jedno źródło wystarcza. Rozszerzenie sources to materiał na
  artykuł panelowy.
- **Sub-dzienna granulacja** - wymaga większego datasetu i specyficznej
  analizy intradayowej. Plan na artykuł nr 2.

## 5. Otwarte decyzje (do rozstrzygnięcia z autorami)

Te trzy wybory wpływają na strukturę artykułu, ale nie blokują
scrapingu:

1. **Etykietowanie sentymentu:** HerBERT off-the-shelf vs lokalne LLM
   (LM Studio) vs słownik vs hybryda z walidacją Cohen's kappa.
2. **Główne pytanie badawcze:** opisowa korelacja vs predykcja
   (Granger) vs event study vs wszystkie trzy.
3. **Walidacja anotatorska:** liczba postów do ręcznego oznaczenia
   (sugestia: 300 z stratyfikacją po długości), pojedynczy vs podwójny
   anotator.

## 6. Reprodukowalność

Każdy wiersz w `posts_raw.csv` zawiera `parser_version` i
`scraped_at_utc`. Każdy plik HTML jest cache'owany w
`cache/bankier_cdr/`. Po zmianie selektora wystarczy:

```bash
# Reparsuj cache bez ponownych requestów do serwera
python cdr_forum_scraper.py --start 2023-01-01 --end 2024-12-31
# (cache hit -> brak ruchu sieciowego, nowa wersja parsera)
```

## 7. Ograniczenia, które należy wymienić w sekcji "Limitations"

- **Selection bias:** użytkownicy bankier.pl to specyficzna populacja
  (głównie inwestorzy detaliczni, polskojęzyczni, emocjonalni).
- **Endogeniczność:** sentyment reaguje na cenę i odwrotnie. Wymaga
  lead/lag i Grangera, nie wystarczy korelacja statyczna.
- **Sarkazm, spam, copy-paste:** sentyment z forum jest noisy; model
  poradzi sobie tylko częściowo.
- **Nietypowy okres:** 2023-2024 dla CDR = pomiędzy Cyberpunk 2.0
  a ogłoszeniem Wiedźmin 4. Dużo zmienności, ale wnioski mogą się
  nie generalizować na okresy "spokojne".
- **Wahania nazwy benchmarku:** WIG.GAMES został przemianowany na
  WIG.GAMES5 w marcu 2022, WIG-GRY powstał w marcu 2022. Ciągłość
  szeregów dla 2023-2024 jest OK, ale ostrożność przy dłuższych
  oknach historycznych.
