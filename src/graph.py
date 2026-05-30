"""
道路ネットワークグラフ管理・経路探索モジュール

起動時に1回だけネットワークをロードし、リクエストごとに Dijkstra を実行。
レスポンスはジオメトリを含まず link_id と到達時間のみ（クライアント側で PMTiles と結合）。
"""

import numpy as np
import geopandas as gpd
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra as sp_dijkstra
from scipy.spatial import KDTree

# 到達時間ランク（10分刻み・0〜9）
def _rank(dist_min: float) -> int:
    return min(int(dist_min // 10), 9)


class RouterGraph:
    """
    vehicle / walk 両モードのグラフを保持し、到達圏・経路探索を提供するクラス。
    """

    def __init__(self, links_path: str, nodes_path: str):
        print(f"[graph] 道路リンク読み込み: {links_path}")
        links = gpd.read_parquet(links_path)
        nodes = gpd.read_parquet(nodes_path)
        print(f"[graph]   リンク: {len(links):,}本  ノード: {len(nodes):,}件")

        links = links.reset_index(drop=True)

        n1 = links["node1"].astype(int).to_numpy()
        n2 = links["node2"].astype(int).to_numpy()

        # ノード ID → 行列インデックス
        unique = np.unique(np.concatenate([n1, n2]))
        self._n2i = {int(n): i for i, n in enumerate(unique.tolist())}
        n_v = len(unique)

        fwd_r = np.array([self._n2i[int(n)] for n in n1], dtype=np.int32)
        fwd_c = np.array([self._n2i[int(n)] for n in n2], dtype=np.int32)
        rows = np.concatenate([fwd_r, fwd_c])
        cols = np.concatenate([fwd_c, fwd_r])

        # vehicle: time_001min × 0.01 (分)
        w_veh  = (links["time_001min"].astype(float) * 0.01).to_numpy(dtype=np.float64)
        # walk  : dist_m / 60 (3.6 km/h = 60 m/min)
        w_walk = (links["dist_m"].astype(float) / 60.0).to_numpy(dtype=np.float64)

        self._G = {
            "vehicle": csr_matrix((np.tile(w_veh,  2), (rows, cols)), shape=(n_v, n_v)),
            "walk":    csr_matrix((np.tile(w_walk, 2), (rows, cols)), shape=(n_v, n_v)),
        }

        # ノードスナップ用 KDTree
        coords   = np.array([[p.y, p.x] for p in nodes.geometry])
        node_ids = nodes["node_id"].astype(int).to_numpy()
        self._kdtree   = KDTree(coords)
        self._node_ids = node_ids

        # リンク両端のノードインデックス（到達圏計算で毎回使う）
        self._link_i1 = np.array([self._n2i.get(int(n), -1) for n in n1], dtype=np.int32)
        self._link_i2 = np.array([self._n2i.get(int(n), -1) for n in n2], dtype=np.int32)
        self._link_valid = (self._link_i1 >= 0) & (self._link_i2 >= 0)

        # (src_idx, dst_idx) → link行インデックス（経路トレース用）
        print("[graph] エッジ逆引きテーブル構築中...")
        self._edge_to_link: dict[tuple, int] = {}
        for i in range(len(n1)):
            i1, i2 = int(self._link_i1[i]), int(self._link_i2[i])
            if i1 >= 0 and i2 >= 0:
                self._edge_to_link[(i1, i2)] = i
                self._edge_to_link[(i2, i1)] = i

        print(f"[graph] 準備完了: ノード {n_v:,} / エッジ {self._G['vehicle'].nnz:,}")

    # ─────────────────────────────────────────────
    # 内部ヘルパー
    # ─────────────────────────────────────────────
    def _snap(self, lat: float, lon: float) -> tuple[int, float]:
        dist_deg, i = self._kdtree.query([lat, lon])
        idx = self._n2i[int(self._node_ids[i])]
        return idx, float(dist_deg * 111_000)

    # ─────────────────────────────────────────────
    # 到達圏（1対N）
    # ─────────────────────────────────────────────
    def reachability(self, lat: float, lon: float, max_min: float, mode: str) -> dict:
        """
        始点から max_min 分以内に到達できる道路リンクの link_id と到達時間を返す。

        Returns:
            {
              "links": {"0": 5.2, "123": 12.1, ...},   # link_id(str) → dist_min(float)
              "meta": { "snap_m": int, "reachable_links": int }
            }
        """
        orig_idx, snap_m = self._snap(lat, lon)
        G = self._G[mode]

        dist_node = sp_dijkstra(G, directed=True, indices=orig_idx,
                                limit=max_min, return_predecessors=False)

        valid = self._link_valid
        d1 = np.where(valid, dist_node[np.where(valid, self._link_i1, 0)], np.inf)
        d2 = np.where(valid, dist_node[np.where(valid, self._link_i2, 0)], np.inf)
        dist_link = np.minimum(d1, d2)

        mask = np.isfinite(dist_link) & (dist_link <= max_min)
        idx_reach = np.where(mask)[0]

        # link_id(str) → dist_min(float, 小数1桁)
        links_dict = {str(int(i)): round(float(dist_link[i]), 1) for i in idx_reach}

        return {
            "links": links_dict,
            "meta": {
                "snap_m":         round(snap_m),
                "reachable_links": int(mask.sum()),
            },
        }

    # ─────────────────────────────────────────────
    # 経路探索（1対1）
    # ─────────────────────────────────────────────
    def route(self, orig_lat: float, orig_lon: float,
              dest_lat: float, dest_lon: float, mode: str) -> dict:
        """
        最短経路の link_id リスト（始点→終点順）を返す。

        Returns:
            {
              "link_ids": [123, 456, ...],
              "meta": { "dist_min": float, "link_count": int, ... }
            }
        """
        orig_idx, orig_snap = self._snap(orig_lat, orig_lon)
        dest_idx, dest_snap = self._snap(dest_lat, dest_lon)
        G = self._G[mode]

        dist, pred = sp_dijkstra(G, directed=True, indices=orig_idx,
                                 return_predecessors=True)

        total_min = float(dist[dest_idx])
        if not np.isfinite(total_min):
            return {
                "link_ids": [],
                "meta": {"error": "到達不能", "dist_min": None},
            }

        # 前ノードをたどって経路リンクを収集
        link_ids: list[int] = []
        node = dest_idx
        while pred[node] != -9999 and node != orig_idx:
            prev = int(pred[node])
            li = self._edge_to_link.get((prev, node))
            if li is not None:
                link_ids.append(li)
            node = prev
        link_ids.reverse()

        return {
            "link_ids": link_ids,
            "meta": {
                "dist_min":    round(total_min, 2),
                "link_count":  len(link_ids),
                "orig_snap_m": round(orig_snap),
                "dest_snap_m": round(dest_snap),
            },
        }
