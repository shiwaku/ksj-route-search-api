#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
縮約グラフ + リンクスナップ + 両端2始点 Dijkstra による到達圏計算

【なぜ2始点か】
縮約でノードが交差点だけになるため、クリック地点を最近傍ノードにスナップすると
最大 +226 m 悪化する（実測）。そこでスナップ先は**元ノード**（72,518件・KDTree 0.4MB）とし、
その元ノードが乗っている縮約リンクの**両端から Dijkstra を1回で回して**、
リンク上のオフセットで合成する。scipy の dijkstra は indices=[a,b] で2始点を1回で処理できる。

  D[v] = min(offset + dA[v], (T - offset) + dB[v])

【出力の粒度】
到達圏は**元リンク単位**で返す（前提③）。対応表の offset を使って
縮約リンク内部の各元リンクのコストを復元するので、縮約前と同じ粒度になる。

【使い方】
  # 縮約前グラフとの一致検証（20地点）
  python3 router.py --case 6441_drm --validate 20

  # 到達圏を1回計算して統計を出す
  python3 router.py --case 6441_drm --lat 43.0687 --lon 141.3508 --limit 30
"""

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import KDTree

BASE = Path(__file__).resolve().parent.parent


def _csr(n1, n2, w, uniq):
    """双方向 CSR を作る。

    ⚠️ `csr_matrix((data, (rows, cols)))` は**重複した (row, col) を合計する**。
    同じノード対を結ぶ並行リンクがあると重みが過大になり、経路が狂う。
    （メッシュ6441 実測: 縮約前 39 対・縮約後 140 対が重複）
    そのため重複は**最小値**に畳んでから渡す。
    """
    i1 = np.searchsorted(uniq, n1).astype(np.int32)
    i2 = np.searchsorted(uniq, n2).astype(np.int32)
    NN = len(uniq)
    rows = np.concatenate([i1, i2])
    cols = np.concatenate([i2, i1])
    data = np.tile(w.astype(np.float64), 2)

    key = rows.astype(np.int64) * NN + cols
    order = np.argsort(key, kind='stable')
    key, data = key[order], data[order]
    starts = np.concatenate([[0], np.flatnonzero(key[1:] != key[:-1]) + 1])
    dmin = np.minimum.reduceat(data, starts)
    kuni = key[starts]
    G = csr_matrix((dmin, (kuni // NN, kuni % NN)), shape=(NN, NN))
    return G, i1, i2


class Router:
    """縮約グラフで到達圏を解き、元リンク単位のコストを返す"""

    def __init__(self, case, net_dir='network', cost='time'):
        self.cost = cost
        col = 'time_001min' if cost == 'time' else 'dist_m'
        self.col = col
        cc = f'{case}_contracted'
        cd = BASE / net_dir / cc
        od = BASE / net_dir / case

        links = gpd.read_parquet(cd / f'KSJ_N13-24_{cc}_道路リンク.parquet')
        self.mem = pd.read_parquet(cd / f'KSJ_N13-24_{cc}_縮約対応表.parquet')
        onodes = gpd.read_parquet(od / f'KSJ_N13-24_{case}_道路ノード.parquet')

        cn1 = links['node1'].astype('int64').to_numpy()
        cn2 = links['node2'].astype('int64').to_numpy()
        self.total = links[col].astype('float64').to_numpy()      # 縮約リンク総コスト
        self.uniq = np.unique(np.concatenate([cn1, cn2]))
        self.G, self.ci1, self.ci2 = _csr(cn1, cn2, self.total, self.uniq)

        # 元ノード → (cid, オフセット) の対応。dict を使わず配列 + searchsorted
        off_col = 'offset_t' if cost == 'time' else 'offset_m'
        len_col = 'len_t' if cost == 'time' else 'len_m'
        m = self.mem
        nf = m['node_from'].to_numpy(); of = m[off_col].to_numpy()
        nt = m['node_to'].to_numpy();   ot = of + m[len_col].to_numpy()
        cid = m['cid'].to_numpy()
        alln = np.concatenate([nf, nt])
        allo = np.concatenate([of, ot])
        allc = np.concatenate([cid, cid])
        order = np.argsort(alln, kind='stable')
        alln, allo, allc = alln[order], allo[order], allc[order]
        first = np.concatenate([[True], alln[1:] != alln[:-1]])    # ノードごとに1件採用
        self.snap_nodes = alln[first]
        self.snap_off = allo[first]
        self.snap_cid = allc[first]

        # スナップ用 KDTree（元ノード座標）
        keep = np.isin(onodes['node_id'].astype('int64').to_numpy(), self.snap_nodes)
        on = onodes[keep]
        self.kd_ids = on['node_id'].astype('int64').to_numpy()
        self.kd = KDTree(np.column_stack([on.geometry.y.to_numpy(),
                                          on.geometry.x.to_numpy()]))

        # 元リンク単位の展開用（対応表を cid 順に整列）
        self.m_cid = m['cid'].to_numpy()
        self.m_off = m[off_col].to_numpy()
        self.m_len = m[len_col].to_numpy()
        self.m_lid = m['link_id'].to_numpy()

    def snap(self, lat, lon):
        dist_deg, k = self.kd.query([lat, lon])
        nid = int(self.kd_ids[k])
        j = int(np.searchsorted(self.snap_nodes, nid))
        return nid, float(dist_deg * 111_000), int(self.snap_cid[j]), float(self.snap_off[j])

    def node_costs(self, cid, off, limit=np.inf):
        """縮約ノードごとの最小コスト D[v]（始点リンクの両端2始点で1回）"""
        a, b = int(self.ci1[cid]), int(self.ci2[cid])
        base_a, base_b = off, self.total[cid] - off
        if a == b:                                    # 閉ループの始点リンク
            d = dijkstra(self.G, directed=True, indices=a, limit=limit)
            return min(base_a, base_b) + d
        d = dijkstra(self.G, directed=True, indices=[a, b], limit=limit)
        return np.minimum(base_a + d[0], base_b + d[1])

    def reachability(self, lat, lon, limit=np.inf):
        """元リンク単位で (link_id[], コスト[], snap_m) を返す（縮約前と同じ粒度）"""
        nid, snap_m, cid, off = self.snap(lat, lon)
        D = self.node_costs(cid, off, limit)

        p = D[self.ci1[self.m_cid]]          # 縮約リンク始点(node1)側のコスト
        q = D[self.ci2[self.m_cid]]          # 縮約リンク終点(node2)側のコスト
        T = self.total[self.m_cid]
        o, l = self.m_off, self.m_len
        s = np.minimum(p + o, q + (T - o))                    # 元リンクの始点
        e = np.minimum(p + o + l, q + (T - o - l))            # 元リンクの終点
        c = np.minimum(s, e)

        # ⚠️ 始点が乗っている縮約リンクの内部は、交差点を経由せず
        #    チェーンに沿って直接たどれる。両端経由だと大幅に過大評価になる。
        sel = self.m_cid == cid
        if sel.any():
            os_, ls_ = o[sel], l[sel]
            along = np.minimum(np.abs(os_ - off), np.abs(os_ + ls_ - off))
            c[sel] = np.minimum(c[sel], along)

        ok = np.isfinite(c) & (c <= limit)
        return self.m_lid[ok], c[ok], snap_m


def ground_truth(case, cost, net_dir='network'):
    """縮約前グラフでの正解（graph.py と同じ min(端点) 方式）"""
    col = 'time_001min' if cost == 'time' else 'dist_m'
    od = BASE / net_dir / case
    links = gpd.read_parquet(od / f'KSJ_N13-24_{case}_道路リンク.parquet').reset_index(drop=True)
    nodes = gpd.read_parquet(od / f'KSJ_N13-24_{case}_道路ノード.parquet')
    n1 = links['node1'].astype('int64').to_numpy()
    n2 = links['node2'].astype('int64').to_numpy()
    w = links[col].astype('float64').to_numpy()
    lid = (links['link_id'].to_numpy() if 'link_id' in links.columns
           else np.arange(len(links)))
    uniq = np.unique(np.concatenate([n1, n2]))
    G, i1, i2 = _csr(n1, n2, w, uniq)
    nid = nodes['node_id'].astype('int64').to_numpy()
    kd = KDTree(np.column_stack([nodes.geometry.y.to_numpy(), nodes.geometry.x.to_numpy()]))

    def solve(lat, lon, limit=np.inf):
        dd, k = kd.query([lat, lon])
        o = int(np.searchsorted(uniq, nid[k]))
        d = dijkstra(G, directed=True, indices=o, limit=limit)
        c = np.minimum(d[i1], d[i2])
        ok = np.isfinite(c) & (c <= limit)
        return lid[ok], c[ok], float(dd * 111_000)

    return solve


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--case', required=True)
    ap.add_argument('--net-dir', default='network')
    ap.add_argument('--cost', default='time', choices=['time', 'dist'])
    ap.add_argument('--limit', type=float, default=np.inf,
                    help='cost=time なら 0.01分単位、dist なら m')
    ap.add_argument('--lat', type=float); ap.add_argument('--lon', type=float)
    ap.add_argument('--validate', type=int, default=0, help='ランダム N 地点で縮約前と比較')
    a = ap.parse_args()

    r = Router(a.case, a.net_dir, a.cost)
    print(f'縮約グラフ: ノード {len(r.uniq):,} / 元リンク {len(r.m_lid):,} / cost={r.col}')

    if a.validate:
        gt = ground_truth(a.case, a.cost, a.net_dir)
        rng = np.random.default_rng(42)
        nodes = gpd.read_parquet(
            BASE / a.net_dir / a.case / f'KSJ_N13-24_{a.case}_道路ノード.parquet')
        idx = rng.choice(len(nodes), a.validate, replace=False)
        lats = nodes.geometry.y.to_numpy()[idx] + rng.normal(0, 0.004, a.validate)
        lons = nodes.geometry.x.to_numpy()[idx] + rng.normal(0, 0.004, a.validate)

        print(f'\n{"#":>3} {"snap(縮約)":>11} {"snap(正解)":>11} {"到達リンク":>12} '
              f'{"最大誤差":>10} {"一致":>6}')
        print('-' * 62)
        worst = 0.0; ng = 0; tc = 0.0; tg = 0.0
        for i, (la, lo) in enumerate(zip(lats, lons)):
            t = time.perf_counter(); L1, C1, s1 = r.reachability(la, lo, a.limit)
            tc += time.perf_counter() - t
            t = time.perf_counter(); L2, C2, s2 = gt(la, lo, a.limit)
            tg += time.perf_counter() - t
            o1 = np.argsort(L1); o2 = np.argsort(L2)
            same = len(L1) == len(L2) and np.array_equal(L1[o1], L2[o2])
            err = float(np.abs(C1[o1] - C2[o2]).max()) if same and len(L1) else np.nan
            worst = max(worst, 0 if np.isnan(err) else err)
            if not same or (err > 1e-6):
                ng += 1
            print(f'{i:>3} {s1:>9.0f} m {s2:>9.0f} m {len(L1):>12,} '
                  f'{err:>10.6f} {"OK" if same and err <= 1e-6 else "NG":>6}')
        print('-' * 62)
        print(f'不一致 {ng} / {a.validate} 地点   最大誤差 {worst:.9f}')
        print(f'平均時間: 縮約 {tc/a.validate*1000:.1f} ms  vs  縮約前 {tg/a.validate*1000:.1f} ms'
              f'  → {tg/tc:.2f} 倍速')

    elif a.lat is not None:
        t = time.perf_counter()
        L, C, s = r.reachability(a.lat, a.lon, a.limit)
        el = time.perf_counter() - t
        print(f'\nsnap {s:.0f} m / 到達リンク {len(L):,} 本 / {el*1000:.1f} ms')
        if len(C):
            print(f'コスト範囲: {C.min():.1f} 〜 {C.max():.1f}')
