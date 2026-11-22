# 道路データの説明スライドの地図: 札幌中心部の国土数値情報 道路データ（N13）を描き、経路探索に使う道路を色で分ける（issue #47）
#   uv run --no-project --with duckdb --with shapely --with matplotlib python docs/foss4g-2026/slides/roads_map.py
# 入力は input/geoparquet/N13-24_6441.parquet（札幌を含む 1 次メッシュ）。出力は shots/ksj_roads_sapporo.png
from pathlib import Path
import duckdb
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
from shapely import from_wkb, get_coordinates, get_num_geometries, get_geometry

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / 'input/geoparquet/N13-24_6441.parquet'
OUT = Path(__file__).parent / 'shots/ksj_roads_sapporo.png'
# 札幌駅のまわり（スライドの枠 5.0 × 5.1 インチに合わせた範囲。経度は cos(43.07°) で縮む）
X0, X1, Y0, Y1 = 141.298, 141.402, 43.025, 43.105
# preprocess/build_network_parquet.py の --filter と同じ: 国道・都道府県道・高速 または 幅員 5.5m 以上
ROAD_CLASS_OK = ('1', '2', '4')
WIDTH_OK = ('3', '4', '5')

con = duckdb.connect()
con.sql('INSTALL spatial; LOAD spatial')
rows = con.sql(f"""
    select N13_003, N13_006, ST_AsWKB(geometry) as wkb from '{SRC.as_posix()}'
    where geometry_bbox.xmax >= {X0} and geometry_bbox.xmin <= {X1}
      and geometry_bbox.ymax >= {Y0} and geometry_bbox.ymin <= {Y1}
""").fetchall()

used, other = [], []
for c3, c6, wkb in rows:
    g = from_wkb(bytes(wkb))
    parts = [get_geometry(g, i) for i in range(get_num_geometries(g))]
    dst = used if (c3 in ROAD_CLASS_OK or c6 in WIDTH_OK) else other
    dst.extend(get_coordinates(p) for p in parts)

fig = plt.figure(figsize=(5.0, 5.1), dpi=300)
ax = fig.add_axes((0, 0, 1, 1))
ax.add_collection(LineCollection(other, colors='#B8BECC', linewidths=0.25))
ax.add_collection(LineCollection(used, colors='#304CF0', linewidths=0.6))
ax.set_xlim(X0, X1)
ax.set_ylim(Y0, Y1)
ax.set_aspect(1 / 0.7307)  # cos(43.07°)
ax.axis('off')
fig.savefig(OUT, facecolor='white')
print('saved', OUT, 'used', len(used), 'other', len(other))
