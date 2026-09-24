#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
縮約前後で Dijkstra の速度を比較する。

到達圏（limit あり・打ち切り）と経路探索（終点まで全探索）の両方を測る。
コストは距離一律（前提②: 速度一律 → dist_m ÷ 速度）で揃える。

  python3 bench_dijkstra.py --cases 6441_drm 6441_drm_contracted
"""

import argparse
import time
import tracemalloc
from pathlib import Path

import numpy as np
import geopandas as gpd
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import KDTree

parser = argparse.ArgumentParser()
parser.add_argument('--cases', nargs='+', required=True)
parser.add_argument('--net-dir', default='network')
parser.add_argument('--limit-m', type=float, default=20_000, help='到達圏の打ち切り距離(m)')
parser.add_argument('--repeat', type=int, default=5)
args = parser.parse_args()

BASE = Path(__file__).resolve().parent.parent

# 札幌駅・稚内あたり（メッシュ6441 内の遠い2点）
ORIG = (43.0687, 141.3508)   # 札幌駅
DEST = (43.2203, 141.7969)   # 当別〜月形方面（同メッシュ内の離れた地点）

results = []

for case in args.cases:
    d = BASE / args.net_dir / case
    lp = d / f'KSJ_N13-24_{case}_道路リンク.parquet'
    npq = d / f'KSJ_N13-24_{case}_道路ノード.parquet'

    tracemalloc.start()
    t0 = time.perf_counter()

    links = gpd.read_parquet(lp).reset_index(drop=True)
    nodes = gpd.read_parquet(npq)
    n1 = links['node1'].astype('int64').to_numpy()
    n2 = links['node2'].astype('int64').to_numpy()
    dist_m = links['dist_m'].astype('float64').to_numpy()

    uniq = np.unique(np.concatenate([n1, n2]))
    NN = len(uniq)
    i1 = np.searchsorted(uniq, n1).astype(np.int32)
    i2 = np.searchsorted(uniq, n2).astype(np.int32)

    rows = np.concatenate([i1, i2])
    cols = np.concatenate([i2, i1])
    w = np.tile(dist_m, 2)
    G = csr_matrix((w, (rows, cols)), shape=(NN, NN))

    node_ids = nodes['node_id'].astype('int64').to_numpy()
    coords = np.column_stack([nodes.geometry.y.to_numpy(), nodes.geometry.x.to_numpy()])
    tree = KDTree(coords)
    build_s = time.perf_counter() - t0
    mem_mb = tracemalloc.get_traced_memory()[0] / 1e6
    tracemalloc.stop()

    def snap(lat, lon):
        _, k = tree.query([lat, lon])
        return int(np.searchsorted(uniq, node_ids[k]))

    o, dst = snap(*ORIG), snap(*DEST)

    # 到達圏（limit あり）
    ts = []
    for _ in range(args.repeat):
        t = time.perf_counter()
        dn = dijkstra(G, directed=True, indices=o, limit=args.limit_m,
                      return_predecessors=False)
        ts.append(time.perf_counter() - t)
    reach_s = float(np.median(ts))
    reached_nodes = int(np.isfinite(dn).sum())

    # 経路探索（終点まで・predecessors あり）
    ts = []
    for _ in range(args.repeat):
        t = time.perf_counter()
        dd, pred = dijkstra(G, directed=True, indices=o, return_predecessors=True)
        ts.append(time.perf_counter() - t)
    route_s = float(np.median(ts))
    route_m = float(dd[dst])

    results.append(dict(case=case, links=len(links), nodes=NN, build_s=build_s,
                        mem_mb=mem_mb, reach_s=reach_s, reached=reached_nodes,
                        route_s=route_s, route_m=route_m))
    print(f'  {case}: 構築 {build_s:.2f}s / 到達圏 {reach_s*1000:.1f}ms / 経路 {route_s*1000:.1f}ms',
          flush=True)

print()
print('===== 結果 =====')
h = f'{"ケース":<28}{"リンク":>10}{"ノード":>10}{"構築":>8}{"メモリ":>10}{"到達圏":>11}{"経路探索":>11}'
print(h)
print('-' * len(h))
for r in results:
    print(f'{r["case"]:<28}{r["links"]:>10,}{r["nodes"]:>10,}'
          f'{r["build_s"]:>7.2f}s{r["mem_mb"]:>9.0f}MB'
          f'{r["reach_s"]*1000:>9.1f}ms{r["route_s"]*1000:>9.1f}ms')

if len(results) == 2:
    a, b = results
    print()
    print(f'  リンク    : {a["links"]/b["links"]:.2f} 倍減')
    print(f'  ノード    : {a["nodes"]/b["nodes"]:.2f} 倍減')
    print(f'  到達圏    : {a["reach_s"]/b["reach_s"]:.2f} 倍速')
    print(f'  経路探索  : {a["route_s"]/b["route_s"]:.2f} 倍速')
    print(f'  メモリ    : {a["mem_mb"]/b["mem_mb"]:.2f} 倍減')
    print()
    print(f'  経路距離の一致確認: {a["route_m"]:,.1f} m vs {b["route_m"]:,.1f} m'
          f'   差 {b["route_m"]-a["route_m"]:+.1f} m')
