"""全国道路ネットワーク parquet → 範囲（bbox）で切り出した小さい parquet（専用エンジンとの経路比較用・bench/compare）

両端の交差点ノードが bbox に入るリンクだけを残す。列は全国版と同じなので、api/graph.py の RoadGraph(case=...) と
preprocess/export_osm_pbf.py --case ... がそのまま読める。

    uv run python preprocess/clip_network.py                                   # → network/tokyo23/（東京 23 区くらい）
    uv run python preprocess/clip_network.py --case foo --bbox 139.0,35.0,139.5,35.5
"""
import argparse
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import shapely

BASE = Path(__file__).resolve().parent.parent
SRC = BASE / 'network' / 'nationwide'
# 東京 23 区を覆う矩形（西端 練馬・世田谷〜東端 江戸川、南端 羽田〜北端 足立）。川崎・埼玉・千葉の一部も入る
TOKYO23 = (139.56, 35.52, 139.92, 35.82)

ap = argparse.ArgumentParser()
ap.add_argument('--case', default='tokyo23')
ap.add_argument('--bbox', default=','.join(map(str, TOKYO23)), help='西,南,東,北（経緯度）')
a = ap.parse_args()
w, s, e, n = map(float, a.bbox.split(','))
out = BASE / 'network' / a.case
out.mkdir(parents=True, exist_ok=True)

t0 = time.perf_counter()
nodes = pq.read_table(SRC / 'KSJ_N13-24_nationwide_道路ノード.parquet')
xy = shapely.get_coordinates(shapely.from_wkb(nodes['geometry'].to_numpy(zero_copy_only=False)))
inside = (xy[:, 0] >= w) & (xy[:, 0] <= e) & (xy[:, 1] >= s) & (xy[:, 1] <= n)
inside_ids = nodes['node_id'].filter(inside)

links = pq.read_table(SRC / 'KSJ_N13-24_nationwide_道路リンク.parquet')
keep = pc.and_(pc.is_in(links['node1'], inside_ids), pc.is_in(links['node2'], inside_ids))
links = links.filter(keep)
used = pc.unique(pa.concat_arrays([links['node1'].combine_chunks(), links['node2'].combine_chunks()]))
nodes = nodes.filter(pc.is_in(nodes['node_id'], used))

# スキーマのメタデータ（geo）ごと書くので GeoParquet のまま
pq.write_table(links, out / f'KSJ_N13-24_{a.case}_道路リンク.parquet')
pq.write_table(nodes, out / f'KSJ_N13-24_{a.case}_道路ノード.parquet')
print(f'{a.case}: links {links.num_rows:,}  nodes {nodes.num_rows:,}  {time.perf_counter()-t0:.1f}s  → {out}')
