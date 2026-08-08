# MRR Radar

Traccia quotidianamente le revenue verificate su [TrustMRR](https://trustmrr.com), calcola il trend di MRR nel tempo e fa emergere in anticipo i prodotti in accelerazione — in fascia di MRR replicabile ($200–$10k).

Zero LLM, zero dipendenze esterne (solo Python 3.11 standard library e HTML/CSS/JS vanilla), zero infrastruttura: GitHub Actions per il cron, git come database, GitHub Pages per la consultazione.

## Come si gira in locale

```bash
python scripts/fetch.py && python scripts/build.py && python -m http.server -d docs
```

Poi apri `http://localhost:8000`. `fetch.py` scarica lo snapshot di oggi (se non esiste già); `build.py` ricalcola tutte le metriche su tutto lo storico e rigenera `docs/data.json`, `docs/report.md` e `reports/<oggi>.md`.

`fetch.py --force` ripete il fetch anche se lo snapshot di oggi esiste già. `build.py` non tocca la rete: si può rieseguire in qualsiasi momento, anche offline, su tutto lo storico.

## Dove si tocca la formula dello score

Il blocco `SCORING` in cima a `scripts/build.py` — pesi di crescita, accelerazione, streak e le fasce di `base_factor` per MRR. È l'unico punto del codice che definisce l'ordinamento; modificarlo e rilanciare `build.py` ricalcola tutto lo storico in un secondo.

## La nota sull'attesa

**Il sistema non ha valore il primo giorno.** Le metriche misurate (`g7`, `g30`, `accel`, `streak`, `score`) richiedono almeno 2 settimane di snapshot per essere calcolate, e diventano affidabili dopo 30 giorni di storico. Fino ad allora ogni record è marcato `"proxy": true` e la dashboard mostra al suo posto `growth30d` / `growthMRR30d` — la crescita *dichiarata* da TrustMRR, non quella *misurata* da noi.

## Natura di `fetch.py`: è uno scraper, non un client API

`fetch.py` non parla con un'API stabile di TrustMRR — legge le pagine HTML pubbliche del sito ed estrae i dati incorporati nel payload RSC di Next.js (dettagli tecnici in `PLAN.md` §2.1). È una tecnica che dipende dai dettagli implementativi interni del sito, dichiaratamente fragile.

Se smette di funzionare, la causa più probabile è che trustmrr.com ha cambiato struttura HTML o framework. Si riparte rileggendo la homepage con `curl` e ripetendo l'ispezione descritta in `PLAN.md` §2.1 — non da un changelog di terzi.

## Struttura

```
scripts/fetch.py   rete → snapshot grezzo (data/snapshots/YYYY-MM-DD.json)
scripts/build.py   snapshot → metriche → docs/data.json + reports/
docs/               root di GitHub Pages: index.html, data.json, report.md
```

## Automazione

`.github/workflows/daily.yml` gira ogni giorno via cron (e a mano via `workflow_dispatch`), esegue fetch + build e committa se qualcosa è cambiato. Nessun secret richiesto: lo scraper legge solo pagine pubbliche. Attiva GitHub Pages da **Settings → Pages → Deploy from a branch → `main` / `/docs`**.
