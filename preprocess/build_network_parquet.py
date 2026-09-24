#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GeoParquet から道路ネットワーク（リンク/ノード parquet）を作る

ksj_to_network_csv.py（GeoJSON・2パス・全リンクをメモリに保持）の置き換え。
計算式（フィルター・haversine 距離・速度テーブル・2次メッシュノードID）は完全に写しており、
**同じ入力に対して同じ出力になることを検証できる**（--verify-against）。

【なぜ作り直したか】
1. 入力が 11.1 GB（GeoJSON）→ 2.6 GB（GeoParquet）。JSON パースも不要
2. 元実装は `parquet_rows` に全リンクを Python dict + shapely で溜めるため、
   全国 1,000万リンクでは 32 GB に収まらない
3. Fortran を使わない（前提③）ので DRM3003 形式の CSV は不要

【違い】
- 出力は parquet のみ（CSV は出さない）
- メッシュ単位で ParquetWriter に逐次書き出す（メモリ一定）
- 列は元実装と同じ: node1 / node2 / mesh2 / N13_002..008 / dist_m / time_001min /
  road_type / geometry。link_id は「行インデックス（0始まり）」という元実装の約束を踏襲

【使い方】
  # 単一メッシュで元実装と一致するか検証
  python3 build_network_parquet.py --mesh 6441 --case 6441_pq --filter \\
      --verify-against 6441_drm

  # 全国
  python3 build_network_parquet.py --nationwide --case nationwide --filter
"""

import argparse
import math
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import geopandas as gpd
from shapely.geometry import LineString, Point

BASE = Path(__file__).resolve().parent.parent

ROAD_CLASS_OK = {'1', '2', '4'}     # 国道・都道府県道・高速自動車国道等
WIDTH_OK = {'3', '4', '5'}          # 幅員5.5m以上
SPEED_KMH = {'1': 35, '2': 30, '3': 20, '4': 80, '5': 20, '6': 20}
ROAD_TYPE = {'1': 3, '2': 5, '3': 7, '4': 1, '5': 7, '6': 7}
CRS = 'EPSG:6668'
R = 6_371_000


def haversine_m(lon1, lat1, lon2, lat2):
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(d_lon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def line_length_m(coords):
    return sum(haversine_m(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
               for i in range(len(coords) - 1))


def mesh2_code(lon, lat):
    p = int(lat * 1.5)
    u = int(lon - 100)
    q = int((lat - p / 1.5) * 12)
    r = int((lon - (100 + u)) * 8)
    return f'{p:02d}{u:02d}{q}{r}'


ap = argparse.ArgumentParser()
g = ap.add_mutually_exclusive_group(required=True)
g.add_argument('--mesh')
g.add_argument('--meshes')
g.add_argument('--nationwide', action='store_true')
ap.add_argument('--case', default=None)
ap.add_argument('--filter', action='store_true')
ap.add_argument('--in-dir', default='input/geoparquet')
ap.add_argument('--net-dir', default='network')
ap.add_argument('--verify-against', default=None,
                help='比較対象のケース名（ksj_to_network_csv.py の出力）')
a = ap.parse_args()

IN_DIR = BASE / a.in_dir
if a.nationwide:
    files = sorted(IN_DIR.glob('N13-24_*.parquet'))
    CASE = a.case or 'nationwide'
else:
    meshes = [a.mesh] if a.mesh else [m.strip() for m in a.meshes.split(',')]
    files = [IN_DIR / f'N13-24_{m}.parquet' for m in meshes]
    CASE = a.case or (meshes[0] if len(meshes) == 1 else 'multi')
missing = [f for f in files if not f.exists()]
if missing:
    raise SystemExit(f'見つかりません: {[str(m) for m in missing][:3]}')

OUT_DIR = BASE / a.net_dir / CASE
OUT_DIR.mkdir(parents=True, exist_ok=True)
P_LINKS = OUT_DIR / f'KSJ_N13-24_{CASE}_道路リンク.parquet'
P_NODES = OUT_DIR / f'KSJ_N13-24_{CASE}_道路ノード.parquet'

t0 = time.time()
print(f'入力 {len(files)} ファイル / フィルター {"あり" if a.filter else "なし"} / ケース {CASE}')

# 座標 → ノードID。キーは (lon,lat) を 1e-9 丸めして int64 にパック（dict を軽くするため）
coord_to_nid: dict[int, str] = {}
mesh_counters: dict[str, int] = {}
LON0, LAT0, SCALE = 100.0, 0.0, 10 ** 7


def pack(lon, lat):
    return ((int(round((lon - LON0) * SCALE)) & 0xFFFFFFFF) << 32) | \
           (int(round((lat - LAT0) * SCALE)) & 0xFFFFFFFF)


import json
from shapely import to_wkb, points
from pyproj import CRS as _CRS


def geo_meta(geom_types):
    """GeoParquet 1.0 の `geo` メタデータ。

    これを最初からスキーマに付けておくことで、書き出し後に
    全件をメモリへ読み戻して付け直す（全国では数十GB）必要がなくなる。
    """
    return {b'geo': json.dumps({
        'version': '1.0.0',
        'primary_column': 'geometry',
        'columns': {'geometry': {
            'encoding': 'WKB',
            'geometry_types': geom_types,
            'crs': json.loads(_CRS.from_user_input(CRS).to_json()),
        }},
    }).encode()}


schema = pa.schema([
    ('node1', pa.string()), ('node2', pa.string()), ('mesh2', pa.string()),
    ('N13_002', pa.string()), ('N13_003', pa.string()), ('N13_004', pa.string()),
    ('N13_005', pa.string()), ('N13_006', pa.string()), ('N13_007', pa.string()),
    ('N13_008', pa.string()),
    ('dist_m', pa.int64()), ('time_001min', pa.int64()), ('road_type', pa.int64()),
    ('geometry', pa.binary()),
], metadata=geo_meta(['LineString']))

writer = None
tot_raw = tot_kept = tot_link = skip_loop = 0
node_coords: list[tuple[float, float]] = []
node_ids: list[str] = []

for fi, fp in enumerate(files, 1):
    gdf = gpd.read_parquet(fp)
    tot_raw += len(gdf)

    if a.filter:
        c3 = gdf['N13_003'].astype(str)
        c6 = gdf['N13_006'].astype(str)
        gdf = gdf[c3.isin(ROAD_CLASS_OK) | c6.isin(WIDTH_OK)]
    tot_kept += len(gdf)
    if len(gdf) == 0:
        continue

    cols = {c: gdf[c].astype(str).to_numpy() for c in
            ('N13_002', 'N13_003', 'N13_004', 'N13_005', 'N13_006', 'N13_007', 'N13_008')}
    geoms = gdf.geometry.to_numpy()

    n1s, n2s, m2s, dms, tms, rts, wkbs = [], [], [], [], [], [], []
    keep_idx = []
    for i, geom in enumerate(geoms):
        coords = list(geom.coords)
        ns = []
        for pt in (coords[0], coords[-1]):
            k = pack(pt[0], pt[1])
            nid = coord_to_nid.get(k)
            if nid is None:
                m = mesh2_code(pt[0], pt[1])
                mesh_counters[m] = mesh_counters.get(m, 0) + 1
                nid = f'{m}{mesh_counters[m]:05d}'
                coord_to_nid[k] = nid
                node_coords.append((pt[0], pt[1]))
                node_ids.append(nid)
            ns.append(nid)
        if ns[0] == ns[1]:
            skip_loop += 1
            continue
        c3v = cols['N13_003'][i]
        dm = max(1, round(line_length_m(coords)))
        sp = SPEED_KMH.get(c3v, 20)
        n1s.append(ns[0]); n2s.append(ns[1])
        m2s.append(mesh2_code(coords[0][0], coords[0][1]))
        dms.append(dm)
        tms.append(max(1, round(dm / sp * 6.0)))
        rts.append(ROAD_TYPE.get(c3v, 7))
        wkbs.append(to_wkb(geom))
        keep_idx.append(i)

    if not keep_idx:
        continue
    ki = np.array(keep_idx)
    tab = pa.table({
        'node1': pa.array(n1s), 'node2': pa.array(n2s), 'mesh2': pa.array(m2s),
        **{c: pa.array(cols[c][ki]) for c in cols},
        'dist_m': pa.array(dms, pa.int64()),
        'time_001min': pa.array(tms, pa.int64()),
        'road_type': pa.array(rts, pa.int64()),
        'geometry': pa.array(wkbs, pa.binary()),
    }, schema=schema)
    if writer is None:
        writer = pq.ParquetWriter(P_LINKS, schema)
    writer.write_table(tab)
    tot_link += len(tab)

    if fi % max(1, len(files) // 20) == 0 or fi == len(files):
        print(f'  {fi:>3}/{len(files)}  生 {tot_raw:>11,}  採用 {tot_kept:>10,}  '
              f'リンク {tot_link:>10,}  ノード {len(coord_to_nid):>10,}  ({time.time()-t0:.0f}s)')

if writer:
    writer.close()

# ノードも WKB を直接書く（shapely のベクトル化 API を使い、Point オブジェクトを作らない）
print('\nノード parquet 出力 ...')
xy = np.asarray(node_coords, dtype=np.float64)
n_schema = pa.schema([('node_id', pa.string()), ('geometry', pa.binary())],
                     metadata=geo_meta(['Point']))
pq.write_table(pa.table({
    'node_id': pa.array(node_ids),
    'geometry': pa.array(to_wkb(points(xy[:, 0], xy[:, 1])), pa.binary()),
}, schema=n_schema), P_NODES)
n_nodes = len(node_ids)

print()
print('===== 完了 =====')
print(f'  生フィーチャ      : {tot_raw:,}')
print(f'  フィルター後      : {tot_kept:,}' + (f'  ({tot_kept/tot_raw*100:.1f}%)' if tot_raw else ''))
print(f'  自己ループ除去    : {skip_loop:,}')
print(f'  リンク            : {tot_link:,}')
print(f'  ノード            : {n_nodes:,}')
print(f'  使用2次メッシュ数 : {len(mesh_counters):,}')
print(f'  経過 {time.time()-t0:.0f}s')
for p in (P_LINKS, P_NODES):
    print(f'  {p.name}  {p.stat().st_size/1e6:.1f} MB')

if a.verify_against:
    print(f'\n===== 検証: {a.verify_against} との一致確認 =====')
    od = BASE / a.net_dir / a.verify_against
    gl = gpd.read_parquet(P_LINKS)
    nodes = gpd.read_parquet(P_NODES)
    ref = gpd.read_parquet(od / f'KSJ_N13-24_{a.verify_against}_道路リンク.parquet')
    rn = gpd.read_parquet(od / f'KSJ_N13-24_{a.verify_against}_道路ノード.parquet')
    ok = True
    for label, x, y in (('リンク数', len(gl), len(ref)), ('ノード数', len(nodes), len(rn))):
        m = x == y
        ok &= m
        print(f'  {label:<12}: {x:,} vs {y:,}  {"一致" if m else "不一致"}')
    for c in ('node1', 'node2', 'dist_m', 'time_001min', 'road_type', 'mesh2'):
        if c in ref.columns and len(gl) == len(ref):
            m = (gl[c].astype(str).to_numpy() == ref[c].astype(str).to_numpy()).all()
            ok &= bool(m)
            print(f'  列 {c:<12}: {"完全一致" if m else "不一致"}')
    if len(gl) == len(ref):
        gm = gl.geometry.geom_equals_exact(ref.geometry, tolerance=0).all()
        ok &= bool(gm)
        print(f'  geometry     : {"完全一致" if gm else "不一致"}')
    print(f'\n  → {"すべて一致" if ok else "差分あり"}')
