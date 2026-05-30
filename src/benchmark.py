#!/usr/bin/env python3
"""
ライブラリ別 Dijkstra 速度ベンチマーク

比較対象:
  - scipy  : sparse CSR + dijkstra (baseline)
  - igraph : C ベース
  - rustworkx : Rust ベース

測定項目:
  - グラフ構築時間
  - SSSP（1対N 到達圏）: 全ノードへの最短距離
  - SSSP with limit（到達圏・max_min 以内に打ち切り）
  - 1対1 経路探索（route）

使い方:
  python3 src/benchmark.py
  python3 src/benchmark.py --links network/saitama/KSJ_N13-24_saitama_all_道路リンク.parquet \
                           --nodes network/saitama/KSJ_N13-24_saitama_all_道路ノード.parquet
"""

import argparse
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
from scipy.spatial import KDTree

REPO_ROOT    = Path(__file__).parent.parent
DEFAULT_LINKS = str(REPO_ROOT / "network/saitama/KSJ_N13-24_saitama_all_道路リンク.parquet")
DEFAULT_NODES = str(REPO_ROOT / "network/saitama/KSJ_N13-24_saitama_all_道路ノード.parquet")

# ベンチマーク始点・終点（埼玉県庁・東松山市役所）
ORIG_LAT, ORIG_LON = 35.8578, 139.6490
DEST_LAT, DEST_LON = 36.0420, 139.4006

LIMIT_MIN = 60.0  # 到達圏の打ち切り閾値（分）


# ─────────────────────────────────────────────────────────
# データ読み込み・共通処理
# ─────────────────────────────────────────────────────────
def load_network(links_path: str, nodes_path: str, mode: str = "vehicle"):
    print(f"道路リンク読み込み中: {links_path}")
    links = gpd.read_parquet(links_path)
    nodes = gpd.read_parquet(nodes_path)
    print(f"  リンク: {len(links):,}本  ノード: {len(nodes):,}件")

    n1 = links["node1"].astype(int).to_numpy()
    n2 = links["node2"].astype(int).to_numpy()

    if mode == "walk":
        # 3.6km/h = 60m/min
        w = (links["dist_m"].astype(float) / 60.0).to_numpy(dtype=np.float64)
    else:
        w = (links["time_001min"].astype(float) * 0.01).to_numpy(dtype=np.float64)

    # 双方向
    src = np.concatenate([n1, n2])
    dst = np.concatenate([n2, n1])
    ws  = np.concatenate([w,  w ])

    unique_nodes = np.unique(np.concatenate([src, dst]))
    n2i = {int(n): i for i, n in enumerate(unique_nodes.tolist())}
    n_v = len(unique_nodes)

    rows = np.array([n2i[int(n)] for n in src], dtype=np.int32)
    cols = np.array([n2i[int(n)] for n in dst], dtype=np.int32)

    # KDTree（ノードスナップ用）
    coords = np.array([[p.y, p.x] for p in nodes.geometry])
    kdtree = KDTree(coords)
    node_ids = nodes["node_id"].astype(int).to_numpy()

    return rows, cols, ws, n2i, n_v, kdtree, node_ids, unique_nodes


def snap_node(kdtree, node_ids, n2i, lat, lon):
    _, i = kdtree.query([lat, lon])
    nid = int(node_ids[i])
    return n2i[nid]


def t(label: str, fn):
    t0 = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - t0
    print(f"  {label:<40} {elapsed:6.2f}s")
    return result, elapsed


# ─────────────────────────────────────────────────────────
# scipy
# ─────────────────────────────────────────────────────────
def bench_scipy(rows, cols, ws, n2i, n_v, kdtree, node_ids, unique_nodes):
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import dijkstra as sp_dijkstra

    print("\n[scipy]")

    G, _ = t("グラフ構築（CSR）",
             lambda: csr_matrix((ws, (rows, cols)), shape=(n_v, n_v)))

    orig_idx = snap_node(kdtree, node_ids, n2i, ORIG_LAT, ORIG_LON)
    dest_idx = snap_node(kdtree, node_ids, n2i, DEST_LAT, DEST_LON)

    dist_full, _ = t("SSSP 全ノード",
        lambda: sp_dijkstra(G, directed=True, indices=orig_idx, return_predecessors=False))

    dist_lim, _ = t(f"SSSP with limit={LIMIT_MIN}分",
        lambda: sp_dijkstra(G, directed=True, indices=orig_idx,
                            limit=LIMIT_MIN, return_predecessors=False))

    def route_scipy():
        dA = sp_dijkstra(G,   directed=True, indices=orig_idx, return_predecessors=False)
        dB = sp_dijkstra(G.T, directed=True, indices=dest_idx, return_predecessors=False)
        return dA[dest_idx]

    route_time, _ = t("経路探索（1対1 双方向）", route_scipy)

    reachable = np.isfinite(dist_full).sum()
    reachable_lim = np.isfinite(dist_lim).sum()
    print(f"    到達可能ノード（全）: {reachable:,}")
    print(f"    到達可能ノード（{LIMIT_MIN}分以内）: {reachable_lim:,}")
    print(f"    最短経路時間: {dist_full[dest_idx]:.1f}分")


# ─────────────────────────────────────────────────────────
# igraph
# ─────────────────────────────────────────────────────────
def bench_igraph(rows, cols, ws, n2i, n_v, kdtree, node_ids, unique_nodes):
    try:
        import igraph as ig
    except ImportError:
        print("\n[igraph] インストールされていません。スキップ。")
        return

    print("\n[igraph]")

    def build():
        edges = list(zip(rows.tolist(), cols.tolist()))
        g = ig.Graph(n=n_v, edges=edges, directed=True)
        g.es["weight"] = ws.tolist()
        return g

    G, _ = t("グラフ構築", build)

    orig_idx = snap_node(kdtree, node_ids, n2i, ORIG_LAT, ORIG_LON)
    dest_idx = snap_node(kdtree, node_ids, n2i, DEST_LAT, DEST_LON)

    dist_full, _ = t("SSSP 全ノード",
        lambda: G.distances(source=orig_idx, weights="weight", mode="out")[0])

    def sssp_limit():
        d = G.distances(source=orig_idx, weights="weight", mode="out")[0]
        return [x if x <= LIMIT_MIN else float("inf") for x in d]

    dist_lim, _ = t(f"SSSP（{LIMIT_MIN}分超を除外）", sssp_limit)

    route_t, _ = t("経路探索（1対1）",
        lambda: G.distances(source=orig_idx, target=dest_idx, weights="weight", mode="out")[0][0])

    reachable = sum(1 for d in dist_full if d != float("inf"))
    reachable_lim = sum(1 for d in dist_lim if d != float("inf") and d <= LIMIT_MIN)
    print(f"    到達可能ノード（全）: {reachable:,}")
    print(f"    到達可能ノード（{LIMIT_MIN}分以内）: {reachable_lim:,}")
    print(f"    最短経路時間: {dist_full[dest_idx]:.1f}分")


# ─────────────────────────────────────────────────────────
# rustworkx
# ─────────────────────────────────────────────────────────
def bench_rustworkx(rows, cols, ws, n2i, n_v, kdtree, node_ids, unique_nodes):
    try:
        import rustworkx as rx
    except ImportError:
        print("\n[rustworkx] インストールされていません。スキップ。")
        return

    print("\n[rustworkx]")

    def build():
        G = rx.PyDiGraph()
        G.add_nodes_from(range(n_v))
        edge_list = [(int(r), int(c), float(w)) for r, c, w in zip(rows, cols, ws)]
        G.add_edges_from(edge_list)
        return G

    G, _ = t("グラフ構築", build)

    orig_idx = snap_node(kdtree, node_ids, n2i, ORIG_LAT, ORIG_LON)
    dest_idx = snap_node(kdtree, node_ids, n2i, DEST_LAT, DEST_LON)

    dist_full, _ = t("SSSP 全ノード",
        lambda: rx.dijkstra_shortest_path_lengths(G, orig_idx, lambda e: e))

    def sssp_limit():
        lengths = rx.dijkstra_shortest_path_lengths(G, orig_idx, lambda e: e)
        return {k: v for k, v in lengths.items() if v <= LIMIT_MIN}

    dist_lim, _ = t(f"SSSP with limit={LIMIT_MIN}分", sssp_limit)

    route_t, _ = t("経路探索（1対1）",
        lambda: rx.dijkstra_shortest_path_lengths(G, orig_idx, lambda e: e, goal=dest_idx))

    reachable = len(dist_full)
    reachable_lim = len(dist_lim)
    dest_dist = dist_full[dest_idx] if dest_idx in dist_full else float("inf")
    print(f"    到達可能ノード（全）: {reachable:,}")
    print(f"    到達可能ノード（{LIMIT_MIN}分以内）: {reachable_lim:,}")
    print(f"    最短経路時間: {dest_dist:.1f}分")


# ─────────────────────────────────────────────────────────
# メイン
# ─────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--links", default=DEFAULT_LINKS)
    ap.add_argument("--nodes", default=DEFAULT_NODES)
    ap.add_argument("--mode", default="vehicle", choices=["vehicle", "walk"])
    args = ap.parse_args()

    print(f"モード: {args.mode}")
    t0 = time.perf_counter()
    data = load_network(args.links, args.nodes, args.mode)
    print(f"  データ読み込み: {time.perf_counter()-t0:.1f}s\n")

    bench_scipy(*data)
    bench_igraph(*data)
    bench_rustworkx(*data)

    print("\n完了")


if __name__ == "__main__":
    main()
