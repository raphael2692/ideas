"""trustmrr.com -> data/snapshots/YYYY-MM-DD.json. Solo rete, nessun calcolo."""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

USER_AGENT = "mrr-radar-bot/1.0 (+https://github.com/raphael2692/ideas)"
BASE_URL = "https://trustmrr.com"
REQUEST_DELAY_S = 0.35
RETRY_DELAYS_S = (1, 4, 16)
MAX_FAILED_PAGES_RATIO = 0.30

# Elencate una volta da /category il 2026-08-08 (cambiano raramente: non ri-scaricare ogni giorno).
CATEGORIES = (
    "ai saas developer-tools fintech marketing ecommerce productivity design-tools no-code "
    "analytics education health-fitness community content-creation crypto-web3 customer-support "
    "entertainment games green-tech iot-hardware legal marketplace mobile-apps news-magazines "
    "real-estate recruiting sales security social-media travel utilities"
).split()

NEXT_CHUNK_RE = re.compile(r'self\.__next_f\.push\((\[.*?\])\)</script>', re.S)
OBJECT_START_RE = re.compile(r'\{"_id":')
class PageFetchError(Exception):
    """Errore di rete/HTTP definitivo su una singola pagina, dopo i retry."""

def fetch_url(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error = None
    for delay in (0,) + RETRY_DELAYS_S:
        if delay:
            time.sleep(delay)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code == 429 or 500 <= e.code < 600:
                continue
            raise PageFetchError(f"{url}: HTTP {e.code}") from e
        except urllib.error.URLError as e:
            last_error = e
            continue
    raise PageFetchError(f"{url}: fallito dopo {len(RETRY_DELAYS_S) + 1} tentativi ({last_error})")

def extract_startups(html: str) -> dict:
    """RSC payload (self.__next_f.push) -> {slug: record}. Vedi PLAN.md §2.1."""
    payload = "".join(json.loads(m)[1] for m in NEXT_CHUNK_RE.findall(html))
    # Uno slug puo' comparire piu' volte (card "trending" parziale + riga
    # completa in tabella): si tiene il primo valore non nullo per campo.
    decoder = json.JSONDecoder()
    startups = {}
    for m in OBJECT_START_RE.finditer(payload):
        try:
            obj, _ = decoder.raw_decode(payload, m.start())
        except json.JSONDecodeError:
            continue
        if not (isinstance(obj, dict) and "slug" in obj):
            continue
        existing = startups.get(obj["slug"])
        if existing is None:
            startups[obj["slug"]] = obj
        else:
            existing.update((k, v) for k, v in obj.items() if v is not None and existing.get(k) is None)
    return startups

def to_cents(value):
    return None if value is None else round(value * 100)

def fetch_page(url: str, label: str, errors: list) -> dict:
    try:
        records = extract_startups(fetch_url(url))
    except PageFetchError as e:
        print(f"[fetch] FALLITA {label}: {e}", file=sys.stderr)
        errors.append(label)
        return {}
    if not records:
        print(f"[fetch] FALLITA {label}: nessun record trovato (pagina probabilmente cambiata)", file=sys.stderr)
        errors.append(label)
        return {}
    print(f"[fetch] OK {label}: {len(records)} record")
    return records

def build_snapshot() -> dict:
    total_pages = 2 + len(CATEGORIES)
    errors = []
    home_records = fetch_page(f"{BASE_URL}/", "/", errors)
    time.sleep(REQUEST_DELAY_S)
    acquire_records = fetch_page(f"{BASE_URL}/acquire", "/acquire", errors)
    time.sleep(REQUEST_DELAY_S)
    category_of_slug = {}
    for category in CATEGORIES:
        records = fetch_page(f"{BASE_URL}/category/{category}", f"/category/{category}", errors)
        for slug in records:
            category_of_slug.setdefault(slug, category)
        time.sleep(REQUEST_DELAY_S)
    if len(errors) / total_pages > MAX_FAILED_PAGES_RATIO:
        raise SystemExit(f"Troppe pagine fallite ({len(errors)}/{total_pages}): {errors}")
    startups = {}
    for slug, rec in home_records.items():
        acquire = acquire_records.get(slug, {})
        founded = acquire.get("foundedDate")
        startups[slug] = {
            "name": rec.get("name"),
            "category": category_of_slug.get(slug),
            "description": (rec.get("description") or "")[:160],
            "foundedDate": founded.split("T", 1)[0] if founded else None,
            "paymentProvider": acquire.get("paymentProvider"),
            "mrr": to_cents(rec.get("currentMrr")),
            "last30Days": to_cents(rec.get("currentLast30DaysRevenue")),
            "total": to_cents(rec.get("currentTotalRevenue")),
            "growth30d": rec.get("cachedGrowth30d"),
            "growthMRR30d": rec.get("cachedGrowthMRR30d"),
            "onSale": bool(rec.get("onSale")),
            "askingPrice": to_cents(acquire.get("askingPrice")),
            "multiple": acquire.get("multiple"),
        }
    return {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "trustmrr-scrape",
        "pages_ok": total_pages - len(errors),
        "pages_failed": len(errors),
        "startups": startups,
    }

def is_valid_snapshot(path: str) -> bool:
    if not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            return len(json.load(f).get("startups") or {}) > 0
    except (json.JSONDecodeError, OSError, AttributeError):
        return False

def write_snapshot(snapshot: dict, path: str) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, sort_keys=True, indent=1, ensure_ascii=False)
    os.replace(tmp_path, path)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--out", default="data/snapshots")
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join(args.out, f"{today}.json")
    if not args.force and is_valid_snapshot(path):
        print(f"[fetch] snapshot di oggi gia' presente ({path}), nessuna richiesta.")
        return
    snapshot = build_snapshot()
    write_snapshot(snapshot, path)
    print(f"[fetch] scritto {path}: {len(snapshot['startups'])} startup, "
          f"{snapshot['pages_ok']} pagine ok, {snapshot['pages_failed']} fallite.")

if __name__ == "__main__":
    main()
