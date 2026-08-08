# MRR Radar — Piano di implementazione

Sistema per tracciare quotidianamente le revenue verificate su TrustMRR, calcolare il trend di MRR nel tempo e far emergere in anticipo i prodotti in accelerazione.

**Vincoli non negoziabili**
- Zero LLM nella pipeline. Tutte le metriche sono aritmetica deterministica.
- Zero dipendenze esterne: solo standard library Python 3.11 e HTML/CSS/JS vanilla. Nessun `pip install`, nessun `npm`, nessuna CDN.
- Zero infrastruttura: GitHub Actions per il cron, git come database, GitHub Pages per la consultazione.
- Output leggibile sia come Markdown versionato sia come web app statica.

---

## 0. Prerequisiti (da fare a mano prima di scrivere codice)

Non si usa l'API a pagamento di TrustMRR. Il fetch è uno **scraper delle pagine pubbliche** del sito (vedi §2 per la tecnica, già verificata a mano il 2026-08-08).

1. Verificare `https://trustmrr.com/robots.txt`: al momento consente tutto (`Allow: /`) e non ha `Crawl-delay`. Ricontrollarlo comunque prima di attivare il cron — può cambiare, e in quel caso lo scraper va fermato finché non lo si rilegge.
2. Nessuna API key, nessun secret da configurare: lo scraper legge solo HTML pubblico, non autenticato.
3. Decidere la visibilità del repo:
   - **Pubblico** → GitHub Pages gratuito + minuti Actions illimitati. Consigliato salvo esigenze di riservatezza.
4. Definire uno `User-Agent` onesto e identificabile, come costante in `fetch.py` (es. `mrr-radar-bot/1.0 (+https://github.com/<owner>/mrr-radar)`). Nessun tentativo di imitare un browser reale: se il sito inizia a bloccare quello User-Agent, è un segnale a cui va obbedito, non aggirato.

---

## 1. Struttura del repository

```
mrr-radar/
├─ .github/workflows/daily.yml
├─ scripts/
│  ├─ fetch.py              # rete → snapshot grezzo. Unico file che parla con trustmrr.com (scraping HTML, no API).
│  └─ build.py              # snapshot → metriche → data.json + markdown. Puro, offline.
├─ data/
│  └─ snapshots/
│     └─ YYYY-MM-DD.json    # append-only, mai modificati né cancellati
├─ reports/
│  └─ YYYY-MM-DD.md         # archivio markdown storico
├─ docs/                    # root di GitHub Pages
│  ├─ index.html            # dashboard, file unico
│  ├─ data.json             # dataset corrente con tutte le metriche
│  └─ report.md             # ultimo report, copia di reports/<oggi>.md
├─ .gitignore
└─ README.md
```

La separazione `fetch` / `build` è deliberata: `build.py` deve poter essere rieseguito su tutto lo storico in qualsiasi momento senza toccare la rete. Quando cambi la formula di scoring, ricalcoli tutto il passato in un secondo.

---

## 2. `scripts/fetch.py`

**Responsabilità:** scaricare pagine HTML pubbliche di trustmrr.com, estrarne i dati incorporati e salvare la fotografia di oggi. Nient'altro. Nessun calcolo.

**Interfaccia CLI**
```
python scripts/fetch.py [--force] [--out data/snapshots]
```
- Se lo snapshot di oggi esiste già ed è valido, esce con codice 0 senza fare richieste, salvo `--force`. Lo script deve poter girare due volte nello stesso giorno senza danni.

### 2.1 Come funziona lo scraping — nota tecnica

trustmrr.com è un'app Next.js (App Router) con rendering lato server: ogni pagina arriva già come HTML completo, verificato con `curl` senza eseguire JavaScript. Dentro l'HTML, tag `<script>self.__next_f.push([1,"..."])</script>` portano il payload RSC ("React Server Components") usato per idratare la pagina lato client. Quel payload contiene, come stringhe JSON incorporate, gli oggetti completi delle startup mostrate — con più campi di quelli visibili nella tabella (es. `currentMrr`, `currentLast30DaysRevenue`, `currentTotalRevenue`, `cachedGrowth30d`, `cachedGrowthMRR30d`, `onSale`, `askingPrice`).

Estrazione, solo standard library:
1. `re.findall(r'self\.__next_f\.push\((\[.*?\])\)</script>', html, re.S)` isola ogni chiamata.
2. `json.loads(match)` su ciascuna: è un array JSON valido a due elementi `[chunk_id, payload_string]`. Questo passaggio fa da solo tutto l'unescaping necessario — nessun parsing manuale di backslash.
3. Concatenare i `payload_string` nell'ordine in cui compaiono, in un unico testo.
4. Nel testo risultante, trovare ogni occorrenza di `{"_id":` e usare `json.JSONDecoder().raw_decode(testo, indice)` per decodificare l'oggetto da lì: gestisce da sola le parentesi graffe annidate, cosa che una regex non farebbe in modo affidabile.
5. Tenere solo gli oggetti con una chiave `slug`.

Questa funzione (`extract_startups(html) -> dict[slug, dict]`) è il cuore del file. Verificata a mano il 2026-08-08: dalla sola homepage estrae ~470 record univoci (contro gli ~82 visibili prima del bottone "Show more", che in realtà non aggiunge dati nuovi — erano già tutti nel payload).

**Fragilità dichiarata, non nascosta:** questa tecnica dipende dai dettagli implementativi interni di Next.js su trustmrr.com, non da un'interfaccia stabile pensata per essere consumata. Se il sito cambia framework, versione, o nomi dei campi, lo scraper si rompe. +Se `extract_startups` non trova nessun record in una pagina che storicamente ne aveva, `fetch.py` deve trattarlo come **fallimento** di quella pagina (stesso meccanismo di retry/skip sotto), mai come "zero startup trovate" silenzioso.

### 2.2 Piano di fetch

Costante in cima al file, non sparsa nel codice:

| # | URL | Cosa fornisce | Record attesi |
|---|-----|----------------|----------------|
| 1 | `/` | Universo principale: MRR, revenue totale, revenue ultimi 30gg, crescita 30gg (revenue e MRR), stato "in vendita" | ~450–650 |
| 2 | `/acquire` | Startup in vendita: `askingPrice`, `multiple`, `userCategory`, `foundedDate`, `paymentProvider` | ~30 |
| 3 | `/category/<slug>` per ciascuna delle categorie note (elencate una volta da `/category`, poi **hardcodate** come costante — non ri-scaricare `/category` ogni giorno, cambia raramente) | Appartenenza a categoria: non è un campo nel record, è implicita nell'URL della pagina in cui compare lo slug | ~30 per categoria, ~30 categorie |

Totale: **~32 richieste al giorno**, contro le ~70 previste per l'API — e senza quota da rispettare. Stessa cortesia di prima: `time.sleep(0.35)` fra una richiesta e l'altra, retry con backoff esponenziale (1s, 4s, 16s) su errori di rete o HTTP 429/5xx, massimo 3 tentativi; su fallimento definitivo di una singola pagina, logga e prosegui — uno snapshot parziale è meglio di nessuno snapshot. Se falliscono più del 30% delle pagine, esci con codice 1 senza scrivere il file, così il workflow segnala rosso.

**Fusione dei dati** in un dizionario per `slug`:
- Record base da `/`.
- `category` sovrascritta con quella dedotta dalla prima pagina `/category/<slug-categoria>` in cui compare lo slug della startup (ordine delle categorie deterministico → risultato riproducibile).
- `askingPrice`, `multiple`, `foundedDate`, `paymentProvider` sovrascritti con i valori da `/acquire`, quando presenti.
- Uno slug visto solo su `/category/...` o `/acquire` ma non su `/` va scartato: senza MRR e crescita non è utilizzabile dal resto della pipeline.

**Deduplica** per `slug`, con precedenza al record incontrato per primo in ciascuna fonte.

### 2.3 Conversione unità — vincolante

I valori monetari sul sito sono in dollari con centesimi in virgola mobile (es. `"currentMrr": 223345.48` = $223.345,48). Nello snapshot vanno convertiti in **interi, centesimi USD**, con `round(valore * 100)` — così lo schema resta identico a quello pensato originariamente per l'API, e `build.py` non cambia. Nessuna divisione per 100 altrove.

### 2.4 Schema dello snapshot

`data/snapshots/2026-08-08.json`:
```json
{
  "fetched_at": "2026-08-08T06:03:11Z",
  "source": "trustmrr-scrape",
  "pages_ok": 31,
  "pages_failed": 1,
  "startups": {
    "shipfast": {
      "name": "ShipFast",
      "category": "saas",
      "description": "…max 160 caratteri…",
      "foundedDate": "2023-09-01",
      "paymentProvider": "stripe",
      "mrr": 180000,
      "last30Days": 4250000,
      "total": 98000000,
      "growth30d": 0.12,
      "growthMRR30d": 8.5,
      "onSale": true,
      "askingPrice": 5000000,
      "multiple": 0.98
    }
  }
}
```
Mappatura campo snapshot → origine scrapata:

| Campo | Origine | Note |
|---|---|---|
| `mrr` | `currentMrr` × 100, arrotondato | |
| `last30Days` | `currentLast30DaysRevenue` × 100 | |
| `total` | `currentTotalRevenue` × 100 | |
| `growth30d` | `cachedGrowth30d` | già in punti percentuali (es. `17.68` = +17,68%) |
| `growthMRR30d` | `cachedGrowthMRR30d` | idem |
| `category` | dedotta da `/category/<slug>` | `null` se lo slug non compare in nessuna pagina categoria |
| `onSale`, `name`, `description` | dal record `/` | |
| `askingPrice`, `multiple`, `foundedDate`, `paymentProvider` | da `/acquire`, solo se in vendita | `null` altrimenti |

Campi presenti nello schema originale pensato per l'API ma **rimossi** perché non ottenibili in modo affidabile su tutto l'universo via scraping: `website` (l'URL del prodotto, non presente in nessuna delle tre fonti — solo il link a `trustmrr.com/startup/<slug>` è garantito), `country`, `customers`, `activeSubscriptions`, `visitorsLast30Days`, `revenuePerVisitor`, `targetAudience`, `rank`, `xHandle`. Recuperarli richiederebbe una richiesta per singola startup (`/startup/<slug>`, 450+ pagine/giorno) e va valutato a parte se servisse davvero — per ora `build.py` (§3) non li usa in nessuna metrica.

Note vincolanti:
- Il campo `source` è `"trustmrr-scrape"`, per essere onesti sul fatto che non è più l'API ufficiale. Un domani un secondo scraper dovrà produrre lo stesso schema.
- Scrivere il JSON con `sort_keys=True, indent=1`: i diff di git restano leggibili e comprimibili.
- Scrivere prima su file temporaneo e poi `os.replace()`, per non lasciare snapshot troncati se il job viene ucciso.

### 2.5 Rete

Solo `urllib.request` (standard library). Nessun `requests`, nessun `beautifulsoup4`, nessun parser HTML esterno: l'estrazione lavora su regex + `json`, non serve un DOM parser. Header `User-Agent` impostato sulla costante definita in §0.5 su ogni richiesta.

---

## 3. `scripts/build.py`

**Responsabilità:** leggere tutti gli snapshot, calcolare le metriche di trend, produrre `docs/data.json`, `reports/<oggi>.md` e le copie in `docs/`.

Deve funzionare anche con **un solo snapshot** disponibile: in quel caso le metriche di trend valgono `null` e il report riporta in testa quanti giorni di storico mancano.

### 3.1 Costruzione delle serie

Per ogni `slug`, costruire la serie ordinata `[(date, mrr, last30Days)]` unendo tutti gli snapshot. Uno slug assente da uno snapshot **non** vale zero: è un buco, e va trattato come dato mancante.

Helper fondamentale: `value_at(series, target_date, tolerance_days=2)` restituisce il punto più vicino a `target_date` entro la tolleranza, altrimenti `None`. Serve perché un job può saltare un giorno e i confronti non devono rompersi.

### 3.2 Metriche per startup

| Metrica | Definizione |
|---|---|
| `mrr_now` | ultimo MRR noto (centesimi) |
| `g7` | variazione % MRR su 7 giorni: `(mrr_now / value_at(T-7) - 1) * 100` |
| `g30` | idem su 30 giorni |
| `g7_prev` | `g7` calcolato fra T-7 e T-14 |
| `accel` | `g7 - g7_prev` — **la seconda derivata, il segnale centrale del sistema** |
| `streak` | numero di snapshot consecutivi, a ritroso, con crescita MRR > 0.5% (soglia per filtrare il rumore di cambio) |
| `weeks_tracked` | numero di settimane di storico disponibili per quello slug |
| `is_new` | `true` se il primo snapshot in cui compare risale a meno di 14 giorni fa |
| `spark` | ultimi 30 punti `[["2026-07-10", 180000], …]` per lo sparkline |

Quando lo storico locale non basta (`weeks_tracked < 2`), usare come proxy dichiarato i campi `growthMRR30d` e `growth30d` scrapati dal sito (§2.4), marcando il record con `"proxy": true`. Il frontend deve distinguerli visivamente: sono dati dichiarati dalla piattaforma, non misurati da noi.

### 3.3 Momentum score

Blocco `SCORING` in cima al file, con pesi come costanti nominate e un commento che spiega ogni scelta. Formula:

```
clamp(x, lo, hi) = max(lo, min(hi, x))

growth_term = clamp(g7, 0, 100) / 100          # crescita recente
accel_term  = clamp(accel, 0, 50) / 50         # accelerazione
streak_term = min(streak, 4) / 4               # persistenza

base_factor:                                    # replicabilità
  mrr < $200            → 0.4    (troppo rumore, un singolo cliente muove il %)
  $200 ≤ mrr < $10k     → 1.0    (fascia bersaglio: validato ma ancora replicabile)
  $10k ≤ mrr < $50k     → 0.6
  mrr ≥ $50k            → 0.25   (interessante da studiare, non da replicare)

score = round(100 * base_factor * (0.40*growth_term + 0.35*accel_term + 0.25*streak_term))
```
`score = null` se `weeks_tracked < 2`. Lo score non è una verità: è un ordinamento. Deve restare in un unico punto del codice, modificabile in trenta secondi.

### 3.4 Momentum di categoria

Il segnale più forte non è la singola startup fortunata, è la categoria in cui **più prodotti crescono insieme**. Per ogni categoria con almeno 3 startup tracciate da ≥ 2 settimane:
- `median_g7`
- `n_growing` = quante hanno `g7 > 10`
- `share_growing` = `n_growing / n_tracked`

Ordinare per `share_growing` e poi `median_g7`. Questa tabella va in cima al report, prima delle singole startup.

### 3.5 Output

**`docs/data.json`** — array piatto di record, ogni record = campi anagrafici + metriche + `spark`. In coda un oggetto `meta` con `generated_at`, `snapshots_count`, `first_snapshot`, `categories`. Tenere il file sotto i 2 MB: se lo supera, ridurre `spark` a 20 punti e troncare le descrizioni a 120 caratteri.

**`reports/YYYY-MM-DD.md`** — Markdown puro, senza HTML, tabelle GFM, importi già convertiti in dollari e formattati (`$1,240`), percentuali con segno (`+18.2%`). Struttura:

```markdown
# MRR Radar — 8 agosto 2026

487 startup tracciate · 34 snapshot · storico dal 6 luglio 2026

## Categorie in movimento
| Categoria | Crescita mediana 7g | In crescita | Tracciate |

## In accelerazione
Le 20 con `accel` più alto e MRR fra $200 e $10k. Colonne:
| # | Prodotto | Categoria | MRR | 7g | Accel | Streak | Score | Link |

## Nuove entrate
Comparse negli ultimi 14 giorni, ordinate per MRR.

## Streak più lunghe
Crescita ininterrotta da almeno 3 snapshot.

## Tabella completa
Tutte le startup con storico sufficiente, ordinate per score.
```
Per ogni riga il link punta a `https://trustmrr.com/startup/<slug>` — è l'unico link garantito: lo scraping non fornisce l'URL del prodotto (§2.4).

**`docs/report.md`** e **`docs/data.json`** sono le copie che serve Pages. `docs/report.md` è identico all'ultimo file in `reports/`.

---

## 4. `docs/index.html`

File unico, nessun framework, nessun font remoto, nessuna build. Carica `data.json` con `fetch()` e renderizza in JS vanilla. Deve funzionare aprendolo con doppio click da filesystem locale quando servito via `python -m http.server`.

**Il lavoro che deve fare:** far vedere in dieci secondi chi sta accelerando in fascia replicabile.

**Funzionalità, in ordine di priorità**
1. Tabella ordinabile per click sull'intestazione: score, MRR, `g7`, `accel`, streak.
2. Filtri persistenti in querystring (così una vista si può salvare nei preferiti): categoria, range MRR con default $200–$10k, `solo streak ≥ 2`, `solo nuove entrate`, `nascondi proxy`.
3. Ricerca testuale su nome e descrizione.
4. Sparkline SVG inline per riga.
5. Riga espandibile con descrizione, revenue totale, `growth30d`/`growthMRR30d` dichiarati, link alla pagina TrustMRR.

**Direzione visiva** — è un registro di dati finanziari, non una landing page. Densità alta, decorazione zero.

- Palette: fondo `#F1F3F2`, superficie `#FFFFFF`, inchiostro `#1B1F23`, secondario `#8A9199`, segnale positivo `#0B6E4F`, allerta `#B4531B`, righe `#E3E6E5`.
- Tipografia: `ui-sans-serif, system-ui` per etichette e testo; `ui-monospace, "SF Mono", monospace` **esclusivamente per i numeri**, con `font-variant-numeric: tabular-nums` così le colonne si allineano otticamente. Nessun font display, nessun serif: la personalità qui la fa la griglia.
- Elemento distintivo: la colonna **Accel** non è un numero, è una glifo a due segmenti che disegna la pendenza della settimana scorsa contro quella corrente. Il lettore vede la seconda derivata come forma prima di leggerla come cifra. È l'unica licenza grafica del progetto: tutto il resto resta muto.
- Qualità minima: responsive fino a 380px (sotto i 700px la tabella diventa una lista di card), focus da tastiera visibile, `prefers-reduced-motion` rispettato, contrasto AA.
- Copy in italiano, tono asciutto. Stato vuoto: "Nessuna startup corrisponde ai filtri. Allarga il range di MRR." Errore di caricamento: "data.json non raggiungibile. Esegui `python scripts/build.py`."

---

## 5. `.github/workflows/daily.yml`

```yaml
name: daily
on:
  schedule:
    - cron: "0 5 * * *"      # 07:00 in Italia d'estate, 06:00 d'inverno
  workflow_dispatch:
permissions:
  contents: write
concurrency:
  group: daily
  cancel-in-progress: false
```

Step, nell'ordine:
1. `actions/checkout@v4`
2. `actions/setup-python@v5` con `python-version: "3.11"` — nessuna cache, nessuna installazione di pacchetti.
3. `python scripts/fetch.py` — nessun secret richiesto, lo scraper legge solo pagine pubbliche.
4. `python scripts/build.py`
5. Commit condizionale:
   ```bash
   git config user.name "mrr-radar"
   git config user.email "actions@github.com"
   git add data reports docs
   git diff --staged --quiet || git commit -m "snapshot $(date -u +%F)"
   git push
   ```

Il cron di GitHub non è puntuale e può saltare esecuzioni su repo inattivi: è esattamente il motivo per cui `value_at()` ha una tolleranza di ±2 giorni. Non aggiungere logica di recupero.

Attivare Pages da **Settings → Pages → Deploy from a branch → `main` / `/docs`**. Nessun workflow di deploy: una cosa in meno che si può rompere.

---

## 6. `README.md`

Breve, operativo: cos'è, come si gira in locale (`python scripts/fetch.py && python scripts/build.py && python -m http.server -d docs`), dove si tocca la formula dello score, e la nota sull'attesa: **il sistema non ha valore il primo giorno.** Le metriche misurate compaiono dopo 14 giorni, diventano affidabili dopo 30. Fino ad allora si legge la colonna proxy, che è ciò che dichiara TrustMRR, non ciò che abbiamo misurato noi.

Deve includere anche una nota sulla natura di `fetch.py`: è uno scraper, non un client di un'API stabile (§2.1). Se smette di funzionare, la causa più probabile è che trustmrr.com ha cambiato struttura HTML — si riparte rileggendo la homepage con `curl` e ripetendo l'ispezione descritta in §2.1, non da un changelog di terzi.

---

## 7. Criteri di accettazione

L'agent considera il lavoro finito solo quando tutti questi punti passano:

- [ ] `pip list` non serve: entrambi gli script girano su una Python 3.11 pulita.
- [ ] `python scripts/fetch.py` due volte di fila esegue tutte le richieste la prima volta e zero la seconda.
- [ ] `python scripts/build.py` gira con un solo snapshot in `data/snapshots/` senza sollevare eccezioni, e il report dice chiaramente che lo storico è insufficiente.
- [ ] `python scripts/build.py` gira con snapshot che hanno un giorno mancante in mezzo, senza eccezioni.
- [ ] Una startup presente ieri e assente oggi non appare con MRR zero né sparisce dallo storico.
- [ ] Nessun valore monetario compare in centesimi nell'output leggibile.
- [ ] Nessun secret o credenziale nel repo: lo scraper non ne usa.
- [ ] `fetch.py` fallisce in modo esplicito (codice 1, messaggio chiaro) se `extract_startups` non trova record dove prima ne trovava — mai un successo silenzioso con dati vuoti.
- [ ] `docs/index.html` si apre e funziona da `python -m http.server -d docs`, anche offline.
- [ ] `docs/data.json` sta sotto i 2 MB.
- [ ] Il workflow gira a mano via `workflow_dispatch` e produce un commit.
- [ ] Il totale di codice Python sta sotto le 400 righe. Se lo supera, c'è troppa astrazione: semplificare.

---

## 8. Fuori scope, deliberatamente

Non implementare, non anticipare, non lasciare hook a mezzo:
- Qualsiasi chiamata a un LLM.
- Database, ORM, migrazioni.
- Autenticazione, utenti, multi-tenancy.
- Notifiche (Telegram, email). TrustMRR ha già un bot Telegram, e comunque il report giornaliero committato è già una notifica: basta seguire il repo.
- Fonti diverse da TrustMRR. Il campo `source` nello snapshot è l'unica concessione al futuro.
- Test unitari formali: bastano i criteri di accettazione eseguiti a mano.