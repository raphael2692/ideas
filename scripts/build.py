"""snapshot -> metriche di trend -> docs/data.json, reports/<oggi>.md, docs/*. Puro, offline."""
import glob
import json
import os
import statistics
from datetime import date, datetime, timedelta, timezone

SNAPSHOTS_DIR = "data/snapshots"
REPORTS_DIR = "reports"
DOCS_DIR = "docs"
STREAK_THRESHOLD_PCT = 0.5     # sotto questa soglia una variazione e' rumore di cambio, non crescita
NEW_WITHIN_DAYS = 14
DATA_JSON_MAX_BYTES = 2_000_000

# Pesi dello score: 40% crescita recente, 35% accelerazione (il segnale che
# distingue "cresce" da "sta accelerando"), 25% persistenza (streak). Il
# base_factor penalizza MRR troppo piccoli (rumore di un singolo cliente) e
# troppo grandi (interessanti da studiare, non piu' replicabili in fascia indie).
SCORING = {
    "w_growth": 0.40,
    "w_accel": 0.35,
    "w_streak": 0.25,
    "growth_cap": 100.0,
    "accel_cap": 50.0,
    "streak_cap": 4,
    "base_factor_bands": [
        (20000, 0.4),      # < $200: troppo rumore
        (1000000, 1.0),    # $200-$10k: fascia bersaglio, validato ma replicabile
        (5000000, 0.6),    # $10k-$50k
        (float("inf"), 0.25),  # >= $50k: da studiare, non da replicare
    ],
}

def clamp(x, lo, hi):
    return max(lo, min(hi, x))
def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def load_snapshots():
    snapshots = []
    for path in sorted(glob.glob(os.path.join(SNAPSHOTS_DIR, "*.json"))):
        day = os.path.basename(path)[:-5]
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        snapshots.append((parse_date(day), data["startups"]))
    return snapshots

def build_series(snapshots):
    """slug -> [(date, mrr, last30Days), ...] ordinata, senza buchi finti a zero."""
    series = {}
    latest = {}
    for day, startups in snapshots:
        for slug, rec in startups.items():
            if rec.get("mrr") is not None:
                series.setdefault(slug, []).append((day, rec["mrr"], rec.get("last30Days")))
            latest[slug] = rec
    return series, latest

def value_at(pairs, target_date, tolerance_days=2):
    """pairs: [(date, value), ...]. Punto piu' vicino a target_date entro la tolleranza."""
    best, best_diff = None, None
    for d, v in pairs:
        diff = abs((d - target_date).days)
        if diff <= tolerance_days and (best_diff is None or diff < best_diff):
            best, best_diff = v, diff
    return best

def pct_change(new, old):
    if new is None or old is None or old == 0:
        return None
    return (new / old - 1) * 100

def base_factor(mrr_cents):
    for threshold, factor in SCORING["base_factor_bands"]:
        if mrr_cents < threshold:
            return factor
    return SCORING["base_factor_bands"][-1][1]

def compute_score(g7, accel, streak, mrr_now):
    g = clamp(g7 or 0, 0, SCORING["growth_cap"]) / SCORING["growth_cap"]
    a = clamp(accel or 0, 0, SCORING["accel_cap"]) / SCORING["accel_cap"]
    s = min(streak, SCORING["streak_cap"]) / SCORING["streak_cap"]
    weighted = SCORING["w_growth"] * g + SCORING["w_accel"] * a + SCORING["w_streak"] * s
    return round(100 * base_factor(mrr_now) * weighted)

def compute_streak(mrr_pairs):
    """Snapshot consecutivi a ritroso con crescita MRR > STREAK_THRESHOLD_PCT."""
    streak = 0
    for i in range(len(mrr_pairs) - 1, 0, -1):
        _, curr = mrr_pairs[i]
        _, prev = mrr_pairs[i - 1]
        if prev and pct_change(curr, prev) is not None and pct_change(curr, prev) > STREAK_THRESHOLD_PCT:
            streak += 1
        else:
            break
    return streak

RAW_PASSTHROUGH = ["name", "category", "description", "foundedDate", "paymentProvider", "onSale",
                    "askingPrice", "multiple", "total", "last30Days", "growth30d", "growthMRR30d"]

def compute_metrics(slug, mrr_series, raw, today):
    mrr_pairs = [(d, v) for d, v, _ in mrr_series]
    last_date, mrr_now = mrr_pairs[-1]
    first_date = mrr_pairs[0][0]
    weeks_tracked = (last_date - first_date).days // 7
    is_new = (today - first_date).days < NEW_WITHIN_DAYS
    v_t7 = value_at(mrr_pairs, last_date - timedelta(days=7))
    v_t14 = value_at(mrr_pairs, last_date - timedelta(days=14))
    g7 = pct_change(mrr_now, v_t7)
    g30 = pct_change(mrr_now, value_at(mrr_pairs, last_date - timedelta(days=30)))
    g7_prev = pct_change(v_t7, v_t14)
    accel = (g7 - g7_prev) if (g7 is not None and g7_prev is not None) else None
    streak = compute_streak(mrr_pairs)
    proxy = weeks_tracked < 2
    if proxy:
        g30 = raw.get("growthMRR30d")
    score = None if proxy else compute_score(g7, accel, streak, mrr_now)
    result = {k: raw.get(k) for k in RAW_PASSTHROUGH}
    result.update(slug=slug, mrr_now=mrr_now, g7=g7, g30=g30, accel=accel, streak=streak,
                  weeks_tracked=weeks_tracked, is_new=is_new, proxy=proxy, score=score,
                  spark=[[d.isoformat(), v] for d, v in mrr_pairs[-30:]])
    return result

def category_momentum(records):
    by_cat = {}
    for r in records:
        if r["category"] and r["weeks_tracked"] >= 2:
            by_cat.setdefault(r["category"], []).append(r)
    rows = []
    for cat, recs in by_cat.items():
        g7s = [r["g7"] for r in recs if r["g7"] is not None]
        if len(recs) < 3 or not g7s:
            continue
        n_growing = sum(1 for g in g7s if g > 10)
        rows.append({"category": cat, "median_g7": statistics.median(g7s), "n_growing": n_growing,
                      "n_tracked": len(recs), "share_growing": n_growing / len(recs)})
    rows.sort(key=lambda r: (r["share_growing"], r["median_g7"]), reverse=True)
    return rows

def usd(cents):
    return "n/d" if cents is None else f"${cents / 100:,.0f}"

def pct(value):
    return "n/d" if value is None else f"{value:+.1f}%"

MESI_IT = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
           "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]

def italian_date(d):
    return f"{d.day} {MESI_IT[d.month - 1]} {d.year}"

def link(r):
    return f"https://trustmrr.com/startup/{r['slug']}"

def md_table(title, subtitle, headers, row_fn, rows, empty_text):
    """row_fn(i, record) -> riga markdown gia' formattata, i a base 1."""
    lines = [f"## {title}", subtitle, "", "| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += [row_fn(i, r) for i, r in enumerate(rows, 1)] if rows else [f"| {empty_text} |" + " |" * (len(headers) - 1)]
    lines.append("")
    return lines

def render_report(day, records, cat_rows, snapshots_count, first_date):
    tracked = [r for r in records if r["weeks_tracked"] >= 2]
    lines = [f"# MRR Radar — {italian_date(day)}", "",
             f"{len(records)} startup tracciate · {snapshots_count} snapshot · storico dal {first_date.isoformat()}"]
    if snapshots_count < 2 or not tracked:
        lines.append("")
        lines.append("**Storico insufficiente**: servono almeno 2 settimane di snapshot per calcolare le metriche di "
                      "trend. Fino ad allora le crescite mostrate sono quelle dichiarate da TrustMRR (colonna proxy).")
    lines.append("")
    lines += md_table(
        "Categorie in movimento", "",
        ["Categoria", "Crescita mediana 7g", "In crescita", "Tracciate"],
        lambda i, r: f"| {r['category']} | {pct(r['median_g7'])} | {r['n_growing']} | {r['n_tracked']} |",
        cat_rows, "Nessuna categoria con storico sufficiente (servono >= 3 startup tracciate da >= 2 settimane)")
    accel_candidates = sorted(
        (r for r in tracked if r["accel"] is not None and 20000 <= r["mrr_now"] < 1000000),
        key=lambda r: r["accel"], reverse=True)
    lines += md_table(
        "In accelerazione", "Le 20 con `accel` piu' alto e MRR fra $200 e $10k.",
        ["#", "Prodotto", "Categoria", "MRR", "7g", "Accel", "Streak", "Score", "Link"],
        lambda i, r: f"| {i} | {r['name']} | {r['category'] or 'n/d'} | "
                     f"{usd(r['mrr_now'])} | {pct(r['g7'])} | {pct(r['accel'])} | {r['streak']} | {r['score']} | {link(r)} |",
        accel_candidates[:20], "nessuno storico sufficiente")
    new_entries = sorted((r for r in records if r["is_new"]), key=lambda r: r["mrr_now"], reverse=True)
    lines += md_table(
        "Nuove entrate", "Comparse negli ultimi 14 giorni, ordinate per MRR.",
        ["Prodotto", "Categoria", "MRR", "Link"],
        lambda i, r: f"| {r['name']} | {r['category'] or 'n/d'} | {usd(r['mrr_now'])} | {link(r)} |",
        new_entries[:30], "nessuna")
    streaks = sorted((r for r in records if r["streak"] >= 3), key=lambda r: r["streak"], reverse=True)
    lines += md_table(
        "Streak piu' lunghe", "Crescita ininterrotta da almeno 3 snapshot.",
        ["Prodotto", "Streak", "MRR", "Link"],
        lambda i, r: f"| {r['name']} | {r['streak']} | {usd(r['mrr_now'])} | {link(r)} |",
        streaks[:30], "nessuna")
    scored = sorted((r for r in tracked if r["score"] is not None), key=lambda r: r["score"], reverse=True)
    lines += md_table(
        "Tabella completa", "Tutte le startup con storico sufficiente, ordinate per score.",
        ["Prodotto", "Categoria", "MRR", "7g", "Accel", "Streak", "Score", "Link"],
        lambda i, r: f"| {r['name']} | {r['category'] or 'n/d'} | {usd(r['mrr_now'])} | {pct(r['g7'])} | "
                     f"{pct(r['accel'])} | {r['streak']} | {r['score']} | {link(r)} |",
        scored, "nessuna")
    return "\n".join(lines).rstrip() + "\n"

def main():
    os.makedirs(REPORTS_DIR, exist_ok=True)
    os.makedirs(DOCS_DIR, exist_ok=True)
    snapshots = load_snapshots()
    if not snapshots:
        raise SystemExit("Nessuno snapshot in data/snapshots/. Esegui prima scripts/fetch.py.")
    today = snapshots[-1][0]
    series, latest = build_series(snapshots)
    records = [compute_metrics(slug, s, latest[slug], today) for slug, s in series.items()]
    cat_rows = category_momentum(records)
    first_date = snapshots[0][0]
    categories = sorted({r["category"] for r in records if r["category"]})
    meta = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "snapshots_count": len(snapshots), "first_snapshot": first_date.isoformat(), "categories": categories}
    payload = {"startups": records, "meta": meta}
    data_json = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    if len(data_json.encode("utf-8")) > DATA_JSON_MAX_BYTES:
        for r in records:
            r["spark"] = r["spark"][-20:]
            r["description"] = (r.get("description") or "")[:120]
        data_json = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    report_md = render_report(today, records, cat_rows, len(snapshots), first_date)
    report_path = os.path.join(REPORTS_DIR, f"{today.isoformat()}.md")
    for path, content in [(os.path.join(DOCS_DIR, "data.json"), data_json),
                           (report_path, report_md), (os.path.join(DOCS_DIR, "report.md"), report_md)]:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    print(f"[build] {len(records)} startup, {len(snapshots)} snapshot, "
          f"{len(data_json)} byte in data.json, report in {report_path}")

if __name__ == "__main__":
    main()
