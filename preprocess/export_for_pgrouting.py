#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
道路リンク parquet を pgRouting 用の CSV に書き出し、同時に scipy 側の正解を出す

pgRouting のテーブルは `source` / `target` に整数のノードIDを要求する。
いまの node1/node2 は '64410000001' のような11桁なので、**0始まりの連番に振り直す**。
scipy 側も同じ連番を使うので、両者の結果を直接突き合わせられる。

`pgr_createTopology` は不要（node1/node2 をすでに持っているため）。

  python3 export_for_pgrouting.py --case 6441_drm --lat 43.0687 --lon 141.3508 \\
      --dest-lat 43.2203 --dest-lon 141.7969 --limit 3000
"""

import argparse
import csv
import time
from pathlib import Path

import numpy as np
import geopandas as gpd
from shapely import to_wkb
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import KDTree

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from router import _csr           # 重複エッジを最小値に畳む CSR ビルダー

BASE = Path(__file__).resolve().parent.parent

ap = argparse.ArgumentParser()
ap.add_argument('--case', required=True)
ap.add_argument('--net-dir', default='network')
ap.add_argument('--out', default=None)
ap.add_argument('--cost', default='time_001min')
ap.add_argument('--lat', type=float, required=True)
ap.add_argument('--lon', type=float, required=True)
ap.add_argument('--dest-lat', type=float, required=True)
ap.add_argument('--dest-lon', type=float, required=True)
ap.add_argument('--limit', type=float, default=3000)
a = ap.parse_args()

d = BASE / a.net_dir / a.case
out = Path(a.out) if a.out else BASE / a.net_dir / a.case / f'pgrouting_{a.case}.csv'

t0 = time.time()
links = gpd.read_parquet(d / f'KSJ_N13-24_{a.case}_道路リンク.parquet').reset_index(drop=True)
nodes = gpd.read_parquet(d / f'KSJ_N13-24_{a.case}_道路ノード.parquet')
print(f'リンク {len(links):,} / ノード {len(nodes):,}  ({time.time()-t0:.1f}s)')

n1 = links['node1'].astype('int64').to_numpy()
n2 = links['node2'].astype('int64').to_numpy()
w = links[a.cost].astype('float64').to_numpy()
uniq = np.unique(np.concatenate([n1, n2]))
src = np.searchsorted(uniq, n1)          # 0始まりの連番
tgt = np.searchsorted(uniq, n2)

# ── CSV 出力（id は parquet の行インデックス = link_id の約束を踏襲）──
print(f'CSV 出力: {out}')
wkb_hex = [g.hex() for g in to_wkb(links.geometry.to_numpy(), hex=False)] \
    if False else [b.hex() for b in to_wkb(links.geometry.to_numpy())]
with out.open('w', newline='') as f:
    wr = csv.writer(f)
    wr.writerow(['id', 'source', 'target', 'cost', 'reverse_cost', 'geom'])
    for i in range(len(links)):
        wr.writerow([i, int(src[i]), int(tgt[i]), float(w[i]), float(w[i]), wkb_hex[i]])
print(f'  {out.stat().st_size/1e6:.1f} MB  ({time.time()-t0:.1f}s)')

# ── scipy 側の正解 ──
G, i1, i2 = _csr(n1, n2, w, uniq)
nid = nodes['node_id'].astype('int64').to_numpy()
kd = KDTree(np.column_stack([nodes.geometry.y.to_numpy(), nodes.geometry.x.to_numpy()]))


def snap(la, lo):
    dd, k = kd.query([la, lo])
    return int(np.searchsorted(uniq, nid[k])), dd * 111_000


o, so = snap(a.lat, a.lon)
t, st = snap(a.dest_lat, a.dest_lon)
print(f'\n始点 idx={o} (snap {so:.0f} m) / 終点 idx={t} (snap {st:.0f} m)')

ts = []
for _ in range(5):
    s = time.perf_counter()
    dn = dijkstra(G, directed=True, indices=o, limit=a.limit)
    ts.append(time.perf_counter() - s)
reach_nodes = int(np.isfinite(dn).sum())
print(f'\n[scipy] 到達圏 limit={a.limit:.0f}: {np.median(ts)*1000:.1f} ms  '
      f'到達ノード {reach_nodes:,}')

ts = []
for _ in range(5):
    s = time.perf_counter()
    dd, pred = dijkstra(G, directed=True, indices=o, return_predecessors=True)
    ts.append(time.perf_counter() - s)
print(f'[scipy] 経路探索(全探索): {np.median(ts)*1000:.1f} ms  '
      f'始点→終点 コスト {dd[t]:.0f} (= {dd[t]/100:.1f} 分)')

print(f"""
───────── SQL 側で使う値 ─────────
  始点ノード : {o}
  終点ノード : {t}
  到達圏 limit: {a.limit:.0f}
  期待値: 到達ノード {reach_nodes:,} / 経路コスト {dd[t]:.0f}
""")
