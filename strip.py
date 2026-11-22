#!/usr/bin/env python3
import gzip
import json
import os
import sqlite3
import sys
import urllib.request
import zlib

STYLE_URL = os.environ.get(
    'ORM_STYLE_URL', 'https://openrailwaymap.app/style.json'
)


def fetch_style():
    last = None
    for _ in range(3):
        try:
            req = urllib.request.Request(
                STYLE_URL,
                headers={
                    'User-Agent': 'emu-log/1.0 (+https://rail.nedifinita.com/)'
                },
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.load(resp)
        except Exception as e:
            last = e
    raise last


def compute_keep_sets(style):
    sources = style.get('sources', {})
    archive_sources = set()
    for sid, s in sources.items():
        if not isinstance(s, dict) or s.get('type') != 'vector':
            continue
        url = s.get('url', '')
        if isinstance(url, str) and not url.startswith('http'):
            archive_sources.add(sid)
        elif isinstance(url, str) and url.startswith('pmtiles://'):
            archive_sources.add(sid)
    keep = {z: set() for z in range(23)}
    for layer in style.get('layers', []):
        if not isinstance(layer, dict):
            continue
        if layer.get('source') not in archive_sources:
            continue
        sl = layer.get('source-layer')
        if not isinstance(sl, str):
            continue
        layout = layer.get('layout')
        if isinstance(layout, dict) and layout.get('visibility') == 'none':
            continue
        minz = float(layer.get('minzoom', 0))
        maxz = float(layer.get('maxzoom', 24))
        for z in range(23):
            if minz < z + 1 and maxz > z:
                keep[z].add(sl)
    return keep


def read_varint(data, i):
    val = 0
    shift = 0
    while True:
        b = data[i]
        i += 1
        val |= (b & 0x7F) << shift
        if b < 0x80:
            return val, i
        shift += 7


def fields(data):
    i, n = 0, len(data)
    while i < n:
        start = i
        tag, i = read_varint(data, i)
        num, wire = tag >> 3, tag & 7
        if wire == 0:
            _, i = read_varint(data, i)
            val = None
        elif wire == 2:
            ln, i = read_varint(data, i)
            val = data[i:i + ln]
            i += ln
        elif wire == 1:
            val = data[i:i + 8]
            i += 8
        elif wire == 5:
            val = data[i:i + 4]
            i += 4
        else:
            raise ValueError(f'bad wire type {wire}')
        yield num, wire, start, i, val


def layer_name(layer):
    for num, wire, s, e, val in fields(layer):
        if num == 1 and wire == 2:
            return val.decode('utf-8')
    return None


def strip_tile(data, keep):
    out = bytearray()
    for num, wire, s, e, val in fields(data):
        if num == 3 and wire == 2 and layer_name(val) not in keep:
            continue
        out += data[s:e]
    return bytes(out)


def gzip_store(data):
    comp = zlib.compressobj(6, zlib.DEFLATED, 31)
    return comp.compress(data) + comp.flush()


def main():
    path = sys.argv[1]
    keep = compute_keep_sets(fetch_style())
    for z in range(23):
        if keep[z]:
            print(f'keep z{z}: {len(keep[z])} layers', flush=True)
    db = sqlite3.connect(path)
    zooms = {}
    for tile_id, z in db.execute('SELECT tile_id, zoom_level FROM map'):
        zooms.setdefault(tile_id, set()).add(z)
    total = db.execute('SELECT count(*) FROM images').fetchone()[0]
    print(f'{total} images', flush=True)
    stats = {}
    done = 0
    updates = []
    for tile_id, tile_data in db.execute(
        'SELECT tile_id, tile_data FROM images'
    ):
        raw = (
            gzip.decompress(tile_data)
            if tile_data[:2] == b'\x1f\x8b'
            else bytes(tile_data)
        )
        tile_keep = set()
        for z in zooms.get(tile_id, set()):
            tile_keep |= keep[z]
        out = strip_tile(raw, tile_keep)
        for num, wire, s, e, val in fields(out):
            if num == 3 and wire == 2:
                assert layer_name(val) in tile_keep
        for z in zooms.get(tile_id, set()):
            st = stats.setdefault(z, [0, 0, 0])
            st[0] += 1
            st[1] += len(raw)
            st[2] += len(out)
        if out != raw:
            updates.append((gzip_store(out), tile_id))
        done += 1
        if len(updates) >= 20000:
            db.executemany(
                'UPDATE images SET tile_data=? WHERE tile_id=?', updates
            )
            db.commit()
            updates.clear()
            print(f'{done}/{total}', flush=True)
    if updates:
        db.executemany('UPDATE images SET tile_data=? WHERE tile_id=?', updates)
        db.commit()
    for z in sorted(stats):
        n, before, after = stats[z]
        print(f'z{z}: {n} tiles, {before} -> {after} bytes', flush=True)
    print('strip done', flush=True)


if __name__ == '__main__':
    main()
