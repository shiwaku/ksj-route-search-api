"""全国道路ネットワーク parquet → OSM PBF（OSRM / Valhalla / GraphHopper との比較用・ADR-1 追記）

scipy と同じ 388 万リンク・同じ速度テーブルを、OSM の way/node として書き出す。
  node: 交差点ノードは node_id をそのまま OSM id に（11 桁・int64 に収まる）。リンク形状の中間頂点は 10^12 + 連番。
  way : id = link_id + 1（OSM id は正・link_id は parquet 行番号）。tags = highway（N13_003 から）, maxspeed（SPEED_KMH）, oneway=no, ksj:class, ksj:link_id
向きは無向グラフなので問わないが、端点がどの交差点ノードかは連結性に直結するので、始点座標を node1 と照合して合わなければ入れ替える。

    uv run --with osmium python preprocess/export_osm_pbf.py            # → network/nationwide/roads_nationwide.osm.pbf
"""
import sys, time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import shapely
import osmium
from osmium.osm import mutable as M

BASE = Path(__file__).resolve().parent.parent
D = BASE / 'network' / 'nationwide'
OUT = D / 'roads_nationwide.osm.pbf'
SPEED_KMH = {'1': 35, '2': 30, '3': 20, '4': 80, '5': 20, '6': 20}      # build_network_parquet.py と同じ
# highway は速度には使わない（maxspeed で与える）。Valhalla の階層分けだけに効く: trunk/primary は最上位（4° タイル）に入り、
# 国道+都道府県道を全部そこに置くと「Exceeded maximum edgeinfo offset」（1 タイル 32 MB 上限）で構築が落ちた（9/11）。国道→secondary、県道→tertiary に下げる
HIGHWAY = {'1': 'secondary', '2': 'tertiary', '3': 'residential', '4': 'motorway', '5': 'unclassified', '6': 'unclassified'}
INTERIOR_BASE = 10 ** 12

t0 = time.perf_counter()
nodes = pq.read_table(D / 'KSJ_N13-24_nationwide_道路ノード.parquet').to_pandas()
node_xy = shapely.get_coordinates(shapely.from_wkb(nodes['geometry'].to_numpy()))
node_ids = nodes['node_id'].astype('int64').to_numpy()
id2idx = pd.Series(np.arange(len(node_ids)), index=node_ids)
print(f'nodes {len(node_ids):,}  {time.perf_counter()-t0:.1f}s', flush=True)

links = pq.read_table(D / 'KSJ_N13-24_nationwide_道路リンク.parquet',
                      columns=['node1', 'node2', 'N13_003', 'geometry']).to_pandas()
n1 = links['node1'].astype('int64').to_numpy(); n2 = links['node2'].astype('int64').to_numpy()
cls = links['N13_003'].to_numpy()
geoms = shapely.from_wkb(links['geometry'].to_numpy())
coords, idx = shapely.get_coordinates(geoms, return_index=True)          # 全頂点 1,808 万点とリンク番号
starts = np.searchsorted(idx, np.arange(len(links)))                      # 各リンクの先頭頂点の位置
ends = np.append(starts[1:], len(idx))
# 始点座標が node1 と合うか（合わなければ node1/node2 を入れ替える）
first_xy = coords[starts]
n1_xy = node_xy[id2idx.loc[n1].to_numpy()]
swap = np.abs(first_xy - n1_xy).max(axis=1) > 1e-6
n1s = np.where(swap, n2, n1); n2s = np.where(swap, n1, n2)
n2_xy = node_xy[id2idx.loc[n2s].to_numpy()]
bad = np.abs(coords[ends - 1] - n2_xy).max(axis=1) > 1e-6
print(f'links {len(links):,}  coords {len(coords):,}  swapped {swap.sum():,}  end-mismatch {bad.sum():,}  {time.perf_counter()-t0:.1f}s', flush=True)
del links, geoms

if OUT.exists():
    OUT.unlink()
w = osmium.SimpleWriter(str(OUT))
# Valhalla はノードが id 昇順でないと "Detected unsorted input data" で落ちる。交差点ノード（< 10^12）を昇順で書き、中間頂点（10^12+連番）を後ろに続ける
for i in np.argsort(node_ids):
    w.add_node(M.Node(id=int(node_ids[i]), location=(float(node_xy[i, 0]), float(node_xy[i, 1]))))
    if i % 1_000_000 == 0:
        print(f'  node {i:,}', flush=True)
print(f'intersection nodes written  {time.perf_counter()-t0:.1f}s', flush=True)

# 中間頂点（各リンクの 2 番目〜最後から 2 番目）
interior_mask = np.ones(len(coords), dtype=bool)
interior_mask[starts] = False; interior_mask[ends - 1] = False
interior_pos = np.nonzero(interior_mask)[0]
interior_id = np.full(len(coords), -1, dtype=np.int64)
interior_id[interior_pos] = INTERIOR_BASE + np.arange(len(interior_pos))
for k, p in enumerate(interior_pos):
    w.add_node(M.Node(id=int(interior_id[p]), location=(float(coords[p, 0]), float(coords[p, 1]))))
    if k % 2_000_000 == 0:
        print(f'  interior {k:,}/{len(interior_pos):,}', flush=True)
print(f'interior nodes written {len(interior_pos):,}  {time.perf_counter()-t0:.1f}s', flush=True)

for li in range(len(n1s)):
    refs = [int(n1s[li])] + [int(x) for x in interior_id[starts[li] + 1: ends[li] - 1]] + [int(n2s[li])]
    c = cls[li]
    w.add_way(M.Way(id=li + 1, nodes=refs,
                    tags={'highway': HIGHWAY.get(c, 'unclassified'), 'maxspeed': str(SPEED_KMH.get(c, 20)),
                          'oneway': 'no', 'ksj:class': str(c), 'ksj:link_id': str(li)}))
    if li % 1_000_000 == 0:
        print(f'  way {li:,}', flush=True)
w.close()
print(f'done {OUT}  {OUT.stat().st_size/1e6:.0f} MB  {time.perf_counter()-t0:.1f}s')
