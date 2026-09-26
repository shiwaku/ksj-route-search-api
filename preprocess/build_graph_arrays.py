#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
道路リンク/ノード parquet → 組み立て済みの探索グラフ（.npy 群 ＋ meta.json）

【なぜ前処理で組み立てるか】（issue #7）
API は起動のたびに parquet から探索グラフを組み立てていて、全国で 1/2 vCPU 29 秒・
Cloudflare の実機では 44 秒掛かっていた。内訳はジオメトリの WKB 変換 7 秒、ノード ID の
重複除去 5 秒、geopandas での読み込み 5 秒、ID の文字列 → 整数 3 秒など、どれも入力が
変わらない限り毎回同じ結果になる。ここで一度だけ組み立てて配列を書き出し、API は起動時に
それを読むだけにする（api/graph.py の RoadGraph._load_prebuilt）。

【出力】
  network/<case>/KSJ_N13-24_<case>_graph/*.npy と meta.json
  meta.json に元の parquet の大きさを入れる。API は手元の parquet と大きさが違えば
  組み立て済みを使わずに parquet から組み立てる（古いまま使わないため）。
  公開版のイメージには parquet を入れず、このディレクトリだけを焼く（deploy/cloudflare/Dockerfile）。

【⚠️ link_id は parquet の行番号】
配列の並びは parquet の行順のまま。parquet・PMTiles と同じ版で作り直すこと。

【使い方】
  uv run python preprocess/build_graph_arrays.py --case nationwide
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api.graph import BASE, RoadGraph, prebuilt_dir  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument('--case', default='nationwide')
ap.add_argument('--net-dir', default='network')
ap.add_argument('--out', default=None, help='既定は network/<case>/KSJ_N13-24_<case>_graph')
args = ap.parse_args()

d = BASE / args.net_dir / args.case
out = Path(args.out) if args.out else prebuilt_dir(d, args.case)

t0 = time.perf_counter()
g = RoadGraph(args.case, args.net_dir, prebuilt=False)
print(f'parquet から組み立て: {g.load_seconds:.1f} 秒（links {g.n_links:,} / nodes {g.n_nodes:,}）')
g.save(out)
size = sum(p.stat().st_size for p in out.glob('*.npy'))
print(f'書き出し: {out}（{size / 2**20:,.0f} MiB）')

# 読み直して、組み立てた直後と同じ配列になっているか確かめる
g2 = RoadGraph(args.case, args.net_dir)
assert g2.source == 'prebuilt', '組み立て済みが使われなかった（meta.json を確認）'
for name, a in g._arrays().items():
    assert np.array_equal(a, g2._arrays()[name]), name
print(f'読み直し: {g2.load_seconds:.1f} 秒・全配列一致（合計 {time.perf_counter() - t0:.0f} 秒）')
