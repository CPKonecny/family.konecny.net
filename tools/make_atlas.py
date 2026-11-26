#!/usr/bin/env python3
"""Make site/static/atlas.json, the outlines the map page (/map/) draws: land with today's country borders, the
large lakes, and the borders between US states, for North America and Europe.

The site makes no third-party requests, so the map cannot use a tile service; it draws these outlines itself.
Everything here is public domain: Natural Earth 1:50m countries and lakes (naturalearthdata.com) and the US
Census Bureau's state boundaries. They were clipped and simplified with mapshaper before this script ran:

    npm install mapshaper world-atlas@2 us-atlas@3
    curl -O https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_lakes.geojson
    mapshaper node_modules/world-atlas/countries-50m.json -target countries -clip bbox=-136,16,46,73 \
        -filter-islands min-area=900km2 -simplify dp 26% keep-shapes -clean -o precision=0.01 countries.geojson
    mapshaper node_modules/us-atlas/states-10m.json -target states -innerlines -simplify dp 14% \
        -o precision=0.01 statelines.geojson
    mapshaper ne_50m_lakes.geojson -clip bbox=-136,16,46,73 -filter 'this.area > 2.5e9 || (this.area > 4e8 &&
        this.centroidX > -95 && this.centroidX < -70 && this.centroidY > 40 && this.centroidY < 50)' \
        -simplify dp 30% keep-shapes -o precision=0.01 lakes.geojson

    python3 tools/make_atlas.py countries.geojson lakes.geojson statelines.geojson > site/static/atlas.json

Each outline is one list of whole numbers: the first point as longitude and latitude in hundredths of a degree,
then the step to each next point. Borders are today's; the page says so.
"""
import json
import sys


def rings(path):
    fc = json.load(open(path, encoding="utf-8"))
    feats = fc["features"] if fc.get("type") == "FeatureCollection" else [{"geometry": g} for g in fc.get("geometries", [fc])]
    for f in feats:
        g = f.get("geometry")
        if not g:
            continue
        t, co = g["type"], g["coordinates"]
        for line in ([co] if t == "LineString" else co if t in ("Polygon", "MultiLineString") else [r for p in co for r in p]):
            yield line


def pack(line):
    pts = [(round(x * 100), round(y * 100)) for x, y in line]
    out, last = [], None
    for p in pts:
        if p == last:
            continue
        out += [p[0], p[1]] if last is None else [p[0] - last[0], p[1] - last[1]]
        last = p
    return out


def main(countries, lakes, states):
    atlas = {
        "about": "Natural Earth 1:50m countries and lakes, US Census Bureau state borders: public domain. Made by tools/make_atlas.py.",
        "land": [pack(r) for r in rings(countries) if len(r) > 3],
        "lakes": [pack(r) for r in rings(lakes) if len(r) > 3],
        "lines": [pack(r) for r in rings(states) if len(r) > 1],
    }
    json.dump(atlas, sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main(*sys.argv[1:4])
