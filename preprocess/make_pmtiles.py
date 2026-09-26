#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
道路リンク parquet → PMTiles（tippecanoe）

【なぜタイルにするか】
到達圏 API はジオメトリを返さない（第9章）。道路の形は静的なので
一度タイルとして配り、API が返す link_id → コストを
MapLibre の `setFeatureState` で重ねて着色する。

【⚠️ link_id は parquet の行番号】
リンク parquet に link_id 列は無く、**行インデックスが link_id** という
暗黙の約束で router.py / export_for_pgrouting.py が動いている。
タイル側と API 側でこの並びがずれると着色が全部狂うため、
ここでも同じ約束（row_group を順に読んだ通し番号）を使い、
`--use-attribute-for-id=link_id` でフィーチャ ID に昇格させる。

【⚠️ link_id = 0 だけフィーチャ ID が付かない】
tippecanoe が `Can't represent too-large feature ID 0` と警告して 0 を採用しない。
`--use-attribute-for-id` は属性を **ID へ移動**する（属性からは消える）ので、
その 1 本はタイル上で link_id を完全に失う。該当は全国 3,881,601 本のうち 1 本だけ。
ID をずらして回避すると router.py / export_for_pgrouting.py と 1 ずれるため、
**ずらさず記録に留める**。タイルに残る属性は `road_type` のみ。

【使い方】
  python3 make_pmtiles.py --case nationwide            # 全国 z4-13
  python3 make_pmtiles.py --case 6441_drm --max-zoom 14
  uv run python preprocess/make_pmtiles.py --case nationwide --wsl   # Windows: tippecanoe は WSL のものを使う
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import shapely

BASE = Path(__file__).resolve().parent.parent

ap = argparse.ArgumentParser()
ap.add_argument('--case', required=True)
ap.add_argument('--net-dir', default='network')
ap.add_argument('--out', default=None)
ap.add_argument('--layer', default='roads')
ap.add_argument('--min-zoom', type=int, default=4)
ap.add_argument('--max-zoom', type=int, default=13)
ap.add_argument('--precision', type=int, default=6, help='座標の小数桁（6桁 ≒ 11cm）')
ap.add_argument('--no-zoom-filter', action='store_true',
                help='ズーム別の道路種別フィルターを外す（デモ用「全道路」タイル。低ズームは密度で間引かれる）')
ap.add_argument('--max-tile-bytes', type=int, default=1_500_000, help='タイル上限（既定 1.5 MB）')
ap.add_argument('--wsl', action='store_true',
                help='tippecanoe を WSL で動かす（Windows 用。出力先は /mnt/<ドライブ>/… に読み替える）')
ap.add_argument('--no-feature-limit', action='store_true',
                help='tippecanoe の 1 タイル 20 万フィーチャ上限を外す（z6-7 で道路の 5〜8 割が間引かれる原因）')
a = ap.parse_args()

d = BASE / a.net_dir / a.case
src = d / f'KSJ_N13-24_{a.case}_道路リンク.parquet'
out = Path(a.out) if a.out else d / f'roads_{a.case}.pmtiles'

pf = pq.ParquetFile(src)
total = pf.metadata.num_rows
print(f'入力: {src.name}  {total:,} リンク / row group {pf.num_row_groups} 個', flush=True)
print(f'出力: {out}  z{a.min_zoom}-{a.max_zoom}', flush=True)

# ズームごとに道路種別で出し分ける（2026-09-09）。
# 全部を入れて --drop-densest-as-needed に任せると、z8 では市区町村道が「点々」にしか残らず、
# 到達圏を塗っても粒に見える。表示側（web）の出し分けと同じ段階にする:
#   z5-8: 高速・国道（road_type 1,3） / z9-10: ＋都道府県道（5） / z11-: 全部
# 【2026-09-26 変更（issue #17・案 D）】以前は z6-10 に県道まで入れていたが、z6-8 のタイルが展開後で最大 4.2 MB・
# 圧縮後 1.3 MB あり、Mapbox Tiling Service の目安（レイヤー 2,500 KB・タイル 500 KB）を超えていた。
# 広域の読み込み（z8 で 3.2 → 0.6 MB）と到達圏の描画（483 → 139 ms）も重かったので、県道は z9 からにした
ZOOM_FILTER = json.dumps({a.layer: [
    'any',
    ['>=', '$zoom', 11],                                          # z11-: 全部
    ['all', ['>=', '$zoom', 9], ['in', 'road_type', 1, 3, 5]],   # z9-10: 高速・国道・都道府県道
    ['all', ['>=', '$zoom', 5], ['<=', '$zoom', 8], ['in', 'road_type', 1, 3]],   # z5-8: 高速・国道
    ['all', ['<=', '$zoom', 4], ['==', 'road_type', 1]],         # z4: 高速のみ（6.8 万本）。1,200 分の全国到達圏を z4-5 で見せるため
]})


def to_wsl(path):
    """C:\\Users\\… → /mnt/c/Users/…（--wsl のとき tippecanoe に渡す出力先）"""
    p = Path(path).resolve()
    return f'/mnt/{p.drive[0].lower()}' + p.as_posix()[len(p.drive):]

cmd = [
    # wsl -e はシェルを通さない。-e なしだと ZOOM_FILTER の $zoom がシェル変数として空に展開され、全部落ちて空のタイルになる
    *(['wsl', '-e', 'tippecanoe', '-o', to_wsl(out)] if a.wsl else ['tippecanoe', '-o', str(out)]), '-l', a.layer,
    '-Z', str(a.min_zoom), '-z', str(a.max_zoom),
    '--use-attribute-for-id=link_id',      # link_id をフィーチャ ID に（setFeatureState 用）
    *([] if a.no_zoom_filter else ['-j', ZOOM_FILTER]),   # ズーム別の道路種別フィルター
    f'--maximum-tile-bytes={a.max_tile_bytes}',  # 既定 500 KB だと東京の z8 タイル 3 枚で県道が間引かれる（26〜78% 残し）
    '--drop-densest-as-needed',            # それでもタイル上限を超えたら間引く（保険）
    '--extend-zooms-if-still-dropping',
    # 低ズームは強めに簡略化し、座標の精度も落とす（-D 10 = 1 タイル 1,024 単位）。最大ズームは従来どおり（4・精度 12）。
    # 見た目はほぼ変わらず、z7 の東京付近で展開後 15.9 → 11.5 MB（#17 案 B）
    '--simplification=10', '--simplification-at-maximum-zoom=4', '-D', '10',
    *(['--no-feature-limit'] if a.no_feature_limit else []),
    '--force',
]
print('  ' + ' '.join(cmd), flush=True)

t0 = time.time()
p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
w = p.stdin
done = 0
try:
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg, columns=['geometry', 'road_type'])
        geom = shapely.from_wkb(t.column('geometry').to_numpy(zero_copy_only=False))
        # 座標を丸めてから GeoJSON 化（shapely の to_geojson に桁指定は無い）
        geom = shapely.transform(geom, lambda c: np.round(c, a.precision))
        gj = shapely.to_geojson(geom)
        rt = t.column('road_type').to_numpy()
        ids = np.arange(done, done + len(gj), dtype=np.int64)   # ← 行番号 = link_id

        buf = ''.join(
            f'{{"type":"Feature","properties":{{"link_id":{i},"road_type":{r}}},'
            f'"geometry":{g}}}\n'
            for i, r, g in zip(ids.tolist(), rt.tolist(), gj)
        )
        w.write(buf.encode())
        done += len(gj)
        print(f'  {done:,} / {total:,} ({done/total*100:.0f}%)  {time.time()-t0:.0f}s',
              flush=True)
    w.close()
except BrokenPipeError:
    print('tippecanoe が終了しました', file=sys.stderr)

rc = p.wait()
el = time.time() - t0
if rc != 0:
    sys.exit(f'tippecanoe が異常終了しました (rc={rc})')
print(f'\n完了: {out}  {out.stat().st_size/1e6:,.0f} MB  {el/60:.1f} 分', flush=True)
