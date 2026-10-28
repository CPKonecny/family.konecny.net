#!/usr/bin/env python3
"""
geocode_backfill.py — put coordinates on the places in Gramps (v1 of the site needs them for the map).

Reads every place through the loopback API, looks up the ones without latitude/longitude on
Nominatim (OpenStreetMap's geocoder, free, one request per second), and writes a review file.
Nothing goes into Gramps until you run it again with --apply.

    python3 geocode_backfill.py                 # look up, write geocode_review.csv + geocode_cache.json
    python3 geocode_backfill.py --apply         # write lat/long for rows with status auto or ok

Review: open geocode_review.csv (it is in ~/gramps-tools/). Rows with status "auto" matched
cleanly. Rows with status "review" need a glance: check lat/lon (or paste better ones from
mapy.cz / OpenStreetMap) and change status to "ok"; or leave them and they are skipped. Rows with
status "none" found nothing. Ancestry's long place strings ("Štítná nad Vláří 131, Zlín, …") are
cleaned before lookup; house numbers are dropped.

Needs an Editor account for --apply (the read-only site-export account is enough for the lookup).
Standard library only. Be a good citizen to Nominatim: keep --sleep at 1.1 or more.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gramps_export import Api, ApiError  # noqa: E402

VERSION = "0.1.0"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
COUNTRY_WORDS = {
    "usa": "United States", "united states of america": "United States", "u.s.a.": "United States",
    "czechia": "Czechia", "czech republic": "Czechia", "bohemia": "Czechia", "moravia": "Czechia",
    "böhmen": "Czechia", "mähren": "Czechia", "austria-hungary": "Czechia", "austrian empire": "Czechia",
    "canada": "Canada", "england": "United Kingdom", "united kingdom": "United Kingdom", "denmark": "Denmark",
    "germany": "Germany", "prussia": "Germany", "massachusetts bay": "United States", "brit": "United States",
}
US_STATES = {"wisconsin", "minnesota", "illinois", "new york", "new jersey", "vermont", "massachusetts", "rhode island",
             "connecticut", "arizona", "california", "florida", "texas", "michigan", "iowa", "ohio", "pennsylvania",
             "indiana", "missouri", "nebraska", "washington", "oregon", "colorado", "north carolina", "virginia"}


def fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn").lower().strip()


def clean_query(name: str) -> tuple[list[str], str | None]:
    """'Svatý Štěpán 35, Zlín, Czechia' -> (['Svatý Štěpán', 'Zlín', 'Czechia'], 'Czechia')"""
    parts = [p.strip() for p in re.split(r"\s*,\s*", name) if p.strip()]
    out = []
    for p in parts:
        p = re.sub(r"\s*\b(ward|precinct|district|township)\s+\d+\b.*$", "", p, flags=re.I).strip()
        p = re.sub(r"\s+(č\.\s*p\.\s*)?\d+[a-z]?$", "", p).strip()            # house numbers
        p = re.sub(r"^(town of|city of|village of)\s+", "", p, flags=re.I).strip()
        if p and p.lower() not in ("n/a", "unknown", "?"):
            out.append(p)
    country = None
    if out:
        last = out[-1].lower()
        if last in COUNTRY_WORDS:
            country = COUNTRY_WORDS[last]
            out[-1] = country
        elif last in US_STATES:
            country = "United States"
            out.append(country)
    return out, country


class Geocoder:
    def __init__(self, cache_path: str, email: str, sleep: float, mock: bool = False):
        self.cache_path, self.email, self.sleep, self.mock = cache_path, email, sleep, mock
        self.cache = {}
        if os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as f:
                self.cache = json.load(f)
        self.last = 0.0

    def save(self):
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=1)

    def query(self, q: str, country: str | None) -> list[dict]:
        key = f"{q}|{country or ''}"
        if key in self.cache:
            return self.cache[key]
        if self.mock:
            res = [{"display_name": q + " (mock)", "lat": "49.0", "lon": "17.9", "importance": 0.6, "class": "place",
                    "type": "village", "name": q.split(",")[0], "address": {"country": country or "Czechia"}}]
            self.cache[key] = res
            return res
        wait = self.sleep - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        params = {"q": q, "format": "jsonv2", "limit": 5, "addressdetails": 1, "accept-language": "en"}
        if country:
            params["countrycodes"] = {"United States": "us", "Czechia": "cz", "Canada": "ca", "United Kingdom": "gb",
                                      "Denmark": "dk", "Germany": "de"}.get(country, "")
            if not params["countrycodes"]:
                del params["countrycodes"]
        url = NOMINATIM + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": f"family.konecny.net geocode backfill ({self.email})"})
        self.last = time.time()
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    res = json.loads(r.read().decode())
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 503) and attempt < 2:
                    time.sleep(10)
                    continue
                raise
        self.cache[key] = res
        self.save()
        return res


def best_match(parts: list[str], country: str | None, results: list[dict]) -> tuple[dict | None, str, str]:
    """-> (result, status auto|review|none, reason)"""
    if not results:
        return None, "none", "no result"
    first = fold(parts[0]) if parts else ""
    scored = []
    for r in results:
        addr = r.get("address") or {}
        rname = fold(r.get("name") or r.get("display_name", "").split(",")[0])
        name_ok = first and (rname == first or first in rname or rname in first)
        country_ok = (not country) or fold(addr.get("country", "")) == fold(country) or country == "United States" and addr.get("country_code") == "us"
        kind_ok = r.get("class") in ("place", "boundary") or r.get("type") in ("administrative", "city", "town", "village", "hamlet", "suburb", "county", "state")
        score = (2 if name_ok else 0) + (1 if country_ok else -2) + (1 if kind_ok else 0) + float(r.get("importance") or 0)
        scored.append((score, name_ok, country_ok, r))
    scored.sort(key=lambda x: -x[0])
    score, name_ok, country_ok, r = scored[0]
    if not country_ok:
        return r, "review", "country differs from the place string"
    if not name_ok:
        return r, "review", f"name differs: got “{r.get('name') or r.get('display_name', '').split(',')[0]}”"
    rivals = [x for x in scored[1:] if x[1] and x[2] and abs(x[0] - score) < 0.5]
    if rivals:
        return r, "review", f"{len(rivals)} other place(s) with the same name"
    return r, "auto", "clean match"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=os.environ.get("GRAMPS_API", "http://127.0.0.1:5055/api"))
    ap.add_argument("--user", default=os.environ.get("GRAMPS_USER"))
    ap.add_argument("--password-file")
    ap.add_argument("--email", default="chris@konecny.net", help="contact in the Nominatim User-Agent (their policy)")
    ap.add_argument("--work", default=os.path.expanduser("~/gramps-tools"), help="where the review CSV and cache live")
    ap.add_argument("--sleep", type=float, default=1.1)
    ap.add_argument("--redo", action="store_true", help="also look up places that already have coordinates")
    ap.add_argument("--apply", action="store_true", help="write rows with status auto/ok from the CSV into Gramps")
    ap.add_argument("--mock", action="store_true", help="no network: fake lookups (pipeline test)")
    args = ap.parse_args(argv)

    csv_path = os.path.join(args.work, "geocode_review.csv")
    cache_path = os.path.join(args.work, "geocode_cache.json")
    os.makedirs(args.work, exist_ok=True)

    user = args.user or input("Gramps user: ")
    if args.password_file:
        with open(args.password_file, encoding="utf-8") as f:
            password = f.read().strip()
    else:
        password = os.environ.get("GRAMPS_PASSWORD") or getpass.getpass("Gramps password: ")
    api = Api(args.api)
    try:
        api.login(user, password)
        places = list(api.paged("places"))
    except ApiError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    by_id = {p.get("gramps_id"): p for p in places}
    by_handle = {p["handle"]: p for p in places}

    def full_name(p):
        parts, seen = [], set()
        cur = p
        while cur and cur["handle"] not in seen and len(parts) < 8:
            seen.add(cur["handle"])
            parts.append((cur.get("name") or {}).get("value") or cur.get("title") or "")
            refs = cur.get("placeref_list") or []
            cur = by_handle.get(refs[0]["ref"]) if refs else None
        return ", ".join(x for x in parts if x)

    if args.apply:
        if not os.path.exists(csv_path):
            print(f"ERROR: {csv_path} not found — run without --apply first", file=sys.stderr)
            return 2
        written = skipped = failed = 0
        with open(csv_path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("status", "").strip().lower() not in ("auto", "ok"):
                    skipped += 1
                    continue
                p = by_id.get(row["id"])
                try:
                    lat, lon = float(row["lat"]), float(row["lon"])
                except (TypeError, ValueError):
                    skipped += 1
                    continue
                if not p or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    skipped += 1
                    continue
                try:
                    obj = api.get(f"places/{p['handle']}")
                except ApiError as e:
                    print(f"  {row['id']}: {e}", file=sys.stderr)
                    failed += 1
                    continue
                if (obj.get("lat") or obj.get("long")) and not args.redo:
                    skipped += 1
                    continue
                obj["lat"], obj["long"] = f"{lat:.6f}", f"{lon:.6f}"
                try:
                    api.send("PUT", f"places/{p['handle']}", obj)
                    written += 1
                except ApiError as e:
                    print(f"  {row['id']}: {e}", file=sys.stderr)
                    failed += 1
        print(f"wrote coordinates to {written} places; skipped {skipped} (status not auto/ok, bad numbers, or already set); "
              f"failed {failed}" + (" — run --apply again" if failed else ""))
        return 0

    geo = Geocoder(cache_path, args.email, args.sleep, mock=args.mock)
    todo = [p for p in places if args.redo or not (p.get("lat") or p.get("long"))]
    print(f"{len(places)} places in Gramps; {len(todo)} without coordinates; about {len(todo) * args.sleep / 60:.0f} min of lookups", file=sys.stderr)
    rows = []
    counts = {"auto": 0, "review": 0, "none": 0}
    for i, p in enumerate(todo, 1):
        name = full_name(p)
        parts, country = clean_query(name)
        result, status, reason = None, "none", "empty name"
        tried = []
        if parts:
            q = ", ".join(parts)
            tried.append(q)
            try:
                res = geo.query(q, country)
            except (urllib.error.URLError, ValueError) as e:
                print(f"  {p.get('gramps_id')}: lookup failed ({e})", file=sys.stderr)
                res = []
            result, status, reason = best_match(parts, country, res)
            if status == "none" and len(parts) > 2:          # drop the most specific part (a hamlet, a parish, a cemetery)
                q2 = ", ".join(parts[1:])
                tried.append(q2)
                res2 = geo.query(q2, country) if not isinstance(res, Exception) else []
                r2, s2, _ = best_match(parts[1:], country, res2)
                if r2:
                    result, status, reason = r2, "review", f"only the wider area matched ({parts[1]})"
        counts[status] += 1
        alt = "; ".join(f"{x.get('display_name', '')[:60]} @ {x.get('lat')},{x.get('lon')}" for x in (geo.cache.get(f"{tried[0]}|{country or ''}", []) if tried else [])[1:4])
        rows.append({"id": p.get("gramps_id"), "name": name, "query": tried[0] if tried else "", "status": status,
                     "lat": result.get("lat", "") if result else "", "lon": result.get("lon", "") if result else "",
                     "matched": (result or {}).get("display_name", "")[:120], "type": (result or {}).get("type", ""),
                     "reason": reason, "alternatives": alt})
        if i % 25 == 0:
            print(f"  {i}/{len(todo)}", file=sys.stderr)
    geo.save()
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "name", "query", "status", "lat", "lon", "matched", "type", "reason", "alternatives"])
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: ({"review": 0, "none": 1, "auto": 2}[r["status"]], r["name"])))
    print(f"\n{len(rows)} places looked up: auto {counts['auto']}, review {counts['review']}, none {counts['none']}")
    print(f"Review file: {csv_path}  (review rows first). Then: python3 geocode_backfill.py --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
