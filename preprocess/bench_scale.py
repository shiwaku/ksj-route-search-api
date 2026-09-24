#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
経路探索エンジンの「対象ネットワーク規模」を実測する

【問い】pgRouting / Valhalla / OSMnx などは、どれくらいの規模を想定した道具なのか。

【軸はエッジ数ではなく「前処理の有無」】
  前処理なし（毎クエリでグラフ全体を探索）: scipy CSR / pgRouting / NetworkX
    → 規模に比例して遅くなる
  前処理あり（階層 / Contraction Hierarchies）: OSRM / Valhalla / GraphHopper
    → 規模が増えてもクエリ時間はほぼ変わらない。代わりに前処理が重い

このスクリプトは**前処理なしの群**を同じデータで横並びにする。
Valhalla 側は KSJ → OSM PBF 変換が要るため対象外。

  python3 bench_scale.py --cases 6441_drm s2_hokkaido ... --nx-budget 120
"""

import argparse
import gc
import os
import subprocess
import time
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import KDTree

BASE = Path(__file__).resolve().parent.parent
LIMIT_001MIN = 3000        # 30 分


def rss_gb():
    out = subprocess.run(['ps', '-o', 'rss=', '-p', str(os.getpid())],
                         capture_output=True, text=True)
    return int(out.stdout.strip()) * 1024 / 1e9


def load(case):
    d = BASE / 'network' / case
    links = pd.read_parquet(d / f'KSJ_N13-24_{case}_道路リンク.parquet',
                            columns=['node1', 'node2', 'time_001min'])
    nodes = gpd.read_parquet(d / f'KSJ_N13-24_{case}_道路ノード.parquet')
    n1 = links['node1'].astype('int64').to_numpy()
    n2 = links['node2'].astype('int64').to_numpy()
    w = links['time_001min'].astype('float64').to_numpy()
    return n1, n2, w, nodes


def origin_index(nodes, uniq, lat0, lon0):
    """始点は全ケースで同一地点（既定: 東京駅）。

    ⚠️ 最初は「領域の重心に最も近いノード」にしていたが、重心が海上に落ちて
    孤立した離島ノードを拾い、全国で到達 3 本という無意味な結果になった。
    規模の比較には**同じ始点を含む入れ子の領域**を使う必要がある。
    """
    lat = nodes.geometry.y.to_numpy()
    lon = nodes.geometry.x.to_numpy()
    kd = KDTree(np.column_stack([lat, lon]))
    dd, k = kd.query([lat0, lon0])
    nid = nodes['node_id'].astype('int64').to_numpy()[k]
    return int(np.searchsorted(uniq, nid)), float(dd * 111_000)


def bench_scipy(n1, n2, w, nodes, lat0, lon0):
    t0 = time.perf_counter()
    uniq = np.unique(np.concatenate([n1, n2]))
    NN = len(uniq)
    i1 = np.searchsorted(uniq, n1).astype(np.int32)
    i2 = np.searchsorted(uniq, n2).astype(np.int32)
    rows = np.concatenate([i1, i2]); cols = np.concatenate([i2, i1])
    data = np.tile(w, 2)
    key = rows.astype(np.int64) * NN + cols
    order = np.argsort(key, kind='stable')
    key, data = key[order], data[order]
    starts = np.concatenate([[0], np.flatnonzero(key[1:] != key[:-1]) + 1])
    G = csr_matrix((np.minimum.reduceat(data, starts),
                    (key[starts] // NN, key[starts] % NN)), shape=(NN, NN))
    build = time.perf_counter() - t0

    o, snap_m = origin_index(nodes, uniq, lat0, lon0)
    ts = []
    for _ in range(3):
        t = time.perf_counter()
        d = dijkstra(G, directed=True, indices=o, limit=LIMIT_001MIN)
        ts.append(time.perf_counter() - t)
    reach = float(np.median(ts)); reached = int(np.isfinite(d).sum())

    t = time.perf_counter()
    dijkstra(G, directed=True, indices=o)          # 打ち切りなし（経路探索相当）
    full = time.perf_counter() - t
    return dict(build_s=build, reach_s=reach, full_s=full, reached=reached,
                nodes=NN, o=o, snap_m=snap_m)


def bench_networkx(n1, n2, w, o_index, uniq, budget):
    """OSMnx の中身。純 Python の dict グラフがどこで破綻するかを見る"""
    import networkx as nx
    t0 = time.perf_counter()
    i1 = np.searchsorted(uniq, n1); i2 = np.searchsorted(uniq, n2)
    G = nx.Graph()
    G.add_weighted_edges_from(zip(i1.tolist(), i2.tolist(), w.tolist()))
    build = time.perf_counter() - t0
    if build > budget:
        return dict(build_s=build, reach_s=None, reached=None, note='構築だけで予算超過')

    t = time.perf_counter()
    d = nx.single_source_dijkstra_path_length(G, o_index, cutoff=LIMIT_001MIN)
    reach = time.perf_counter() - t
    return dict(build_s=build, reach_s=reach, reached=len(d), note='')


ap = argparse.ArgumentParser()
ap.add_argument('--cases', nargs='+', required=True)
ap.add_argument('--nx-budget', type=float, default=120, help='NetworkX に許す秒数')
ap.add_argument('--lat', type=float, default=35.681236, help='始点（既定: 東京駅）')
ap.add_argument('--lon', type=float, default=139.767125)
a = ap.parse_args()

rows = []
nx_alive = True
for case in a.cases:
    n1, n2, w, nodes = load(case)
    print(f'\n===== {case}: {len(n1):,} リンク =====', flush=True)

    base = rss_gb()
    s = bench_scipy(n1, n2, w, nodes, a.lat, a.lon)
    s_mem = rss_gb() - base
    print(f'  scipy    構築 {s["build_s"]:.2f}s / 到達圏 {s["reach_s"]*1000:.1f}ms / '
          f'全探索 {s["full_s"]*1000:.0f}ms / 到達ノード {s["reached"]:,} / '
          f'snap {s["snap_m"]:.0f}m / +{s_mem:.2f}GB', flush=True)

    n = dict(build_s=None, reach_s=None, reached=None, note='予算超過のため省略')
    if nx_alive:
        uniq = np.unique(np.concatenate([n1, n2]))
        gc.collect(); base = rss_gb()
        n = bench_networkx(n1, n2, w, s['o'], uniq, a.nx_budget)
        n['mem'] = rss_gb() - base
        if n['reach_s'] is None:
            print(f'  NetworkX 構築 {n["build_s"]:.1f}s → {n["note"]}', flush=True)
            nx_alive = False
        else:
            print(f'  NetworkX 構築 {n["build_s"]:.1f}s / 到達圏 {n["reach_s"]*1000:.0f}ms / '
                  f'到達 {n["reached"]:,} / +{n.get("mem",0):.2f}GB', flush=True)
            if n['build_s'] + n['reach_s'] > a.nx_budget:
                print('  → 次の規模は予算超過が確実なので NetworkX を打ち切る', flush=True)
                nx_alive = False
        del uniq
    else:
        print('  NetworkX 省略（前の規模で予算超過）', flush=True)

    rows.append(dict(case=case, links=len(n1), nodes=s['nodes'], scipy=s, scipy_mem=s_mem, nx=n))
    del n1, n2, w, nodes; gc.collect()

print('\n\n===== 規模 vs クエリ時間（到達圏30分・同一データ） =====')
h = (f'{"ケース":<14}{"リンク":>11}{"scipy構築":>11}{"scipy到達圏":>13}'
     f'{"scipy全探索":>13}{"NX構築":>10}{"NX到達圏":>12}{"NX/scipy":>10}')
print(h); print('-' * len(h.encode('utf-8')) // 2 * '-' if False else '-' * 95)
for r in rows:
    s, n = r['scipy'], r['nx']
    nxb = f'{n["build_s"]:.1f}s' if n['build_s'] else '—'
    nxr = f'{n["reach_s"]*1000:.0f}ms' if n['reach_s'] else '—'
    ratio = f'{n["reach_s"]/s["reach_s"]:.0f}倍' if n['reach_s'] else '—'
    print(f'{r["case"]:<14}{r["links"]:>11,}{s["build_s"]:>10.2f}s'
          f'{s["reach_s"]*1000:>12.1f}ms{s["full_s"]*1000:>12.0f}ms{nxb:>10}{nxr:>12}{ratio:>10}')
