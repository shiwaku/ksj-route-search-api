#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全国道路グラフ（in-memory）

探索グラフをプロセスに常駐させる。pgRouting との実測比較で
到達圏 300倍・経路探索 12.5倍の差がつき、結果は完全一致したため
「グラフは in-memory、PostgreSQL はアプリデータ」を採用した。

【link_id は parquet の行番号】
リンク parquet に link_id 列は無い。preprocess/router.py・export_for_pgrouting.py・
make_pmtiles.py と同じく、**行インデックスを link_id とする**約束で揃える。

【2026-09-09 の変更】
- スナップ対象を「連結成分 1,000 ノード以上」に限定。幹線フィルターの副作用で
  県庁前の道路が 2〜4 ノードの孤島になっており、そこに寄ると「到達 2 リンク」が 200 で返っていた
- CSR の構造（indices / indptr）を 1 本にし、重み配列だけを cost × 高速あり/なし の 4 本持つ
- 到達圏は road_class（all / trunk / major / auto）で絞り、上限は分ではなく **リンク 100 万本**
"""

import json
import math
import os
import time
from pathlib import Path

import numpy as np
# pandas / geopandas / pyarrow / shapely は parquet から組み立てるときだけ使うので、その関数の中で import する。
# 組み立て済みを読む公開版では import しない（1/2 vCPU で 2.5 秒掛かっていた）
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import KDTree

BASE = Path(__file__).resolve().parent.parent

COST_COL = {'time': 'time_001min', 'dist': 'dist_m'}
# time_001min は 0.01 分単位の整数。API 境界では「分」に直して返す
COST_SCALE = {'time': 0.01, 'dist': 1.0}

# N13_003 道路分類（build_network_parquet.py の ROAD_CLASS_OK と同じ読み）
#   1=国道  2=都道府県道  3=市区町村道等  4=高速自動車国道等  5・6=その他
EXPRESSWAY = '4'
ROAD_CLASS = {'trunk': ('1', '2', '4'), 'major': ('1', '4')}
ROAD_CLASS_ORDER = ('all', 'trunk', 'major')          # auto はこの順に試す

MIN_COMPONENT_NODES = 1_000     # これ未満の成分にはスナップしない（14 成分・96.3% が残る）
TIER_OK_MAX = 400_000           # 🟢 快適（ヒープ +225 MB・〜0.5 秒）
# 🔴 これを超える結果は返さない（ヒープ +560 MB・〜1.3 秒）。
# デモで「480 分・全道路 244 万本」を見せたいときだけ ROUTE_LINKS_HARD_MAX=5000000 で外す（ADR-4）
LINKS_HARD_MAX = int(os.environ.get('ROUTE_LINKS_HARD_MAX', 1_000_000))
# /route の探索上限の初期値 = 直線距離 km × これ ＋ 15 分（届かなければ倍にして ROUTE_LIMIT_TRIES 回まで試す）。
# 東京→大阪は直線 400 km・379.6 分（0.95 分/km）。高速なしは一般道だけなので大きめに見る
ROUTE_MIN_PER_KM = {True: 1.0, False: 1.6}
ROUTE_LIMIT_TRIES = 3

# 組み立て済みグラフ（preprocess/build_graph_arrays.py が書き出す .npy 群）の形式の版。
# 保存する配列を変えたら上げる。meta.json の版が違えば読まずに parquet から組み立てる
PREBUILT_FORMAT = 1
# 保存する配列。w_* は cost × 高速あり/なし の 4 本の重み（CSR 構造 indices / indptr は共有）
PREBUILT_ARRAYS = ('indices', 'indptr', 'w_time_exp', 'w_time_noexp', 'w_dist_exp', 'w_dist_noexp',
                   'i1', 'i2', 'is_expressway', 'mask_trunk', 'mask_major', 'component',
                   'kd_idx', 'kd_lat', 'kd_lon', 'link_dist', 'link_time', 'pk_order', 'coords', 'coord_offsets')


class TooManyLinks(Exception):
    """road_class を明示指定して上限を超えたとき。counts に各種別の到達本数を持つ（400 の detail 用）"""

    def __init__(self, applied, counts):
        self.applied, self.counts = applied, counts
        alt = '、'.join(f'road_class={k} なら {v:,} 本' for k, v in counts.items() if v <= LINKS_HARD_MAX)
        super().__init__(f'到達リンクが上限 {LINKS_HARD_MAX:,} 本を超えました（{counts[applied]:,} 本）'
                         + (f'。{alt}' if alt else ''))


def _csr_structure(n1, n2, uniq):
    """双方向 CSR の**構造**だけを作る。重みは _weights() で何本でも載せ替えられる。

    ⚠️ csr_matrix は重複した (row, col) を**合計する**ので、並行リンク（中央分離帯で上下線が別、
    ループ橋など・全国 2,746 対）は最小値に畳む。メッシュ6441 で最大 31.45 分の誤差が出た。
    """
    i1 = np.searchsorted(uniq, n1).astype(np.int32)
    i2 = np.searchsorted(uniq, n2).astype(np.int32)
    NN = len(uniq)
    rows = np.concatenate([i1, i2])
    cols = np.concatenate([i2, i1])
    key = rows.astype(np.int64) * NN + cols
    order = np.argsort(key, kind='stable')                    # key 昇順 = CSR の並び
    ks = key[order]
    starts = np.concatenate([[0], np.flatnonzero(ks[1:] != ks[:-1]) + 1])
    kuni = ks[starts]
    indices = (kuni % NN).astype(np.int32)
    indptr = np.concatenate([[0], np.cumsum(np.bincount(kuni // NN, minlength=NN))]).astype(np.int64)
    link_of_entry = np.tile(np.arange(len(n1)), 2)[order]      # 各エントリ（重複込み）の元リンク
    return i1, i2, indices, indptr, starts, link_of_entry


def _load_geometry(path):
    """リンク parquet の geometry 列 → (座標 float32 [N,2] (lon,lat), リンクごとの offsets [L+1])"""
    import pyarrow.parquet as pq
    import shapely
    pf = pq.ParquetFile(path)
    chunks, counts = [], []
    for rg in range(pf.num_row_groups):
        wkb = pf.read_row_group(rg, columns=['geometry']).column(0).to_numpy(zero_copy_only=False)
        geoms = shapely.from_wkb(wkb)
        xy, idx = shapely.get_coordinates(geoms, return_index=True)
        chunks.append(xy.astype(np.float32))
        counts.append(np.bincount(idx, minlength=len(geoms)))
    coords = np.concatenate(chunks)
    offsets = np.concatenate([[0], np.cumsum(np.concatenate(counts))]).astype(np.int64)
    return coords, offsets


def _parquet_paths(d, case):
    return d / f'KSJ_N13-24_{case}_道路リンク.parquet', d / f'KSJ_N13-24_{case}_道路ノード.parquet'


def prebuilt_dir(d, case):
    """組み立て済みグラフの置き場所。版名は parquet・PMTiles と揃える"""
    return d / f'KSJ_N13-24_{case}_graph'


def _source_signature(d, case):
    """組み立ての元にした parquet の大きさ。変わっていれば組み立て済みグラフは古い"""
    return {p.name: p.stat().st_size for p in _parquet_paths(d, case) if p.exists()}


class RoadGraph:
    """探索グラフ。起動時に 1 回作る。

    組み立て済みの配列（prebuilt_dir）があればそれを読むだけ（公開版・issue #7）。無ければ parquet から組み立てる
    （全国で 1/2 vCPU 29 秒・本番 44 秒掛かっていた）。組み立て済みは preprocess/build_graph_arrays.py で作る。
    """

    def __init__(self, case='nationwide', net_dir='network', prebuilt=True):
        t0 = time.perf_counter()
        d = BASE / net_dir / case
        self.case = case
        pre = prebuilt_dir(d, case)
        if prebuilt and self._prebuilt_usable(pre, d, case):
            self._load_prebuilt(pre)
            self.source = 'prebuilt'
        else:
            self._build(d, case)
            self.source = 'parquet'
        self._finish()
        self.load_seconds = time.perf_counter() - t0

    # ------------------------------------------------------------------ 組み立て
    def _build(self, d, case):
        import geopandas as gpd
        import pandas as pd
        link_path, node_path = _parquet_paths(d, case)
        # ジオメトリ列を読まない（290 MB の WKB を触らずに済む）ので pandas 側を使う
        links = pd.read_parquet(link_path, columns=['node1', 'node2', 'dist_m', 'time_001min', 'N13_003'])
        nodes = gpd.read_parquet(node_path)

        n1 = links['node1'].astype('int64').to_numpy()
        n2 = links['node2'].astype('int64').to_numpy()
        self.n_links = len(links)
        uniq = np.unique(np.concatenate([n1, n2]))
        self.n_nodes = len(uniq)

        # --- 道路種別マスク（road_class・use_expressway 用）
        c3 = links['N13_003'].to_numpy()
        self.is_expressway = c3 == EXPRESSWAY
        self._mask_trunk = np.isin(c3, ROAD_CLASS['trunk'])
        self._mask_major = np.isin(c3, ROAD_CLASS['major'])

        # --- CSR 構造 1 本 ＋ 重み配列 4 本（cost × 高速あり/なし）。構造は共有、data だけ違う
        self.i1, self.i2, self._indices, self._indptr, starts, link_of_entry = _csr_structure(n1, n2, uniq)
        self._w = {}
        for cost, col in COST_COL.items():
            w = links[col].to_numpy().astype(np.float64)
            self._w[(cost, True)] = self._weights(w, starts, link_of_entry)
            self._w[(cost, False)] = self._weights(np.where(self.is_expressway, np.inf, w), starts, link_of_entry)

        # --- 連結成分（孤島はスナップ対象から外すため）
        G = csr_matrix((self._w[('time', True)], self._indices, self._indptr), shape=(self.n_nodes, self.n_nodes))
        _, self.component = connected_components(G, directed=False)
        component_size = np.bincount(self.component)[self.component]
        eligible = component_size >= MIN_COMPONENT_NODES

        # --- KDTree の対象はスナップ対象ノードだけ
        nid = nodes['node_id'].astype('int64').to_numpy()
        gidx = np.searchsorted(uniq, nid).astype(np.int32)
        keep = eligible[gidx]
        self.kd_idx = gidx[keep]
        self.kd_lat = nodes.geometry.y.to_numpy()[keep]
        self.kd_lon = nodes.geometry.x.to_numpy()[keep]

        # --- /route 用: ノード対 → リンクの逆引き（並行リンクは複数返るので重みで選ぶ）
        self.link_dist = links['dist_m'].to_numpy().astype(np.int64)
        self.link_time = links['time_001min'].to_numpy().astype(np.int64)
        self._pk_order = np.argsort(self._pair_keys(), kind='stable')

        # --- /route 用: 全リンクの座標を平坦な float32 で常駐（設計書 未確定① → 案(a) 採用）
        # geopandas で読むと 20 秒だが、row group ごとに shapely.from_wkb すれば 1.2 秒・145 MB。
        # 1 経路（約 8,000 本）の取り出しは 3 ms。float32 の丸めは約 1 m で表示用途には十分
        self.coords, self.coord_offsets = _load_geometry(link_path)
        self._source = _source_signature(d, case)

    # ------------------------------------------------------------------ 組み立て済みの保存・読み込み
    def _arrays(self):
        return {
            'indices': self._indices, 'indptr': self._indptr,
            'w_time_exp': self._w[('time', True)], 'w_time_noexp': self._w[('time', False)],
            'w_dist_exp': self._w[('dist', True)], 'w_dist_noexp': self._w[('dist', False)],
            'i1': self.i1, 'i2': self.i2, 'is_expressway': self.is_expressway,
            'mask_trunk': self._mask_trunk, 'mask_major': self._mask_major, 'component': self.component,
            'kd_idx': self.kd_idx, 'kd_lat': self.kd_lat, 'kd_lon': self.kd_lon,
            'link_dist': self.link_dist, 'link_time': self.link_time, 'pk_order': self._pk_order,
            'coords': self.coords, 'coord_offsets': self.coord_offsets,
        }

    def save(self, out_dir):
        """組み立て済みグラフを .npy 群 ＋ meta.json で書き出す（preprocess/build_graph_arrays.py から呼ぶ）"""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for name, a in self._arrays().items():
            np.save(out / f'{name}.npy', np.ascontiguousarray(a))
        meta = {'format': PREBUILT_FORMAT, 'case': self.case, 'n_links': self.n_links, 'n_nodes': self.n_nodes,
                'source': self._source}
        (out / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')

    @staticmethod
    def _prebuilt_usable(pre, d, case):
        """組み立て済みがあり、形式の版が合い、元の parquet が（手元にあれば）同じ大きさなら使う。
        公開版のイメージには parquet を入れないので、parquet が無いときは組み立て済みを信じる"""
        meta_path = pre / 'meta.json'
        if not meta_path.exists():
            return False
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        if meta.get('format') != PREBUILT_FORMAT:
            return False
        src = _source_signature(d, case)
        return not src or src == meta.get('source')

    def _load_prebuilt(self, pre):
        meta = json.loads((pre / 'meta.json').read_text(encoding='utf-8'))
        a = {name: np.load(pre / f'{name}.npy') for name in PREBUILT_ARRAYS}
        self.n_links, self.n_nodes, self._source = meta['n_links'], meta['n_nodes'], meta['source']
        self._indices, self._indptr = a['indices'], a['indptr']
        self._w = {('time', True): a['w_time_exp'], ('time', False): a['w_time_noexp'],
                   ('dist', True): a['w_dist_exp'], ('dist', False): a['w_dist_noexp']}
        self.i1, self.i2, self.is_expressway = a['i1'], a['i2'], a['is_expressway']
        self._mask_trunk, self._mask_major, self.component = a['mask_trunk'], a['mask_major'], a['component']
        self.kd_idx, self.kd_lat, self.kd_lon = a['kd_idx'], a['kd_lat'], a['kd_lon']
        self.link_dist, self.link_time, self._pk_order = a['link_dist'], a['link_time'], a['pk_order']
        self.coords, self.coord_offsets = a['coords'], a['coord_offsets']

    # ------------------------------------------------------------------ 組み立て・読み込みの共通の仕上げ
    def _finish(self):
        """どちらの経路でも同じ、安い後処理（CSR 行列・マスク・成分・KD 木・逆引き）"""
        shape = (self.n_nodes, self.n_nodes)
        self.graphs = {k: csr_matrix((w, self._indices, self._indptr), shape=shape) for k, w in self._w.items()}
        self.G = self.graphs[('time', True)]      # 既定（後方互換）
        self.class_mask = {'all': np.ones(self.n_links, dtype=bool),
                           'trunk': self._mask_trunk, 'major': self._mask_major}

        sizes = np.bincount(self.component)
        self.n_components = len(sizes)
        self.component_size = sizes[self.component]           # ノードごとの所属成分サイズ
        # 成分の名前（到達不能の説明用）。大きい順に 本土・北海道・沖縄、それ以外は離島
        rank = np.argsort(sizes)[::-1]
        self._component_name = {int(rank[0]): '本州・四国・九州', int(rank[1]): '北海道', int(rank[2]): '沖縄本島'}

        self.kd = KDTree(np.column_stack([self.kd_lat, self.kd_lon]))
        self.n_snap_nodes = len(self.kd_idx)
        self._pk_sorted = self._pair_keys()[self._pk_order]

    def _pair_keys(self):
        return np.minimum(self.i1, self.i2).astype(np.int64) * self.n_nodes + np.maximum(self.i1, self.i2)

    @staticmethod
    def _weights(w_link, starts, link_of_entry):
        """リンク重み → CSR エントリ重み（並行リンクは最小値に畳む）"""
        return np.minimum.reduceat(w_link[link_of_entry], starts)

    # ------------------------------------------------------------------ 探索
    def snap(self, lat, lon):
        """最近傍の道路ノード（成分 1,000 ノード以上）へ寄せる。(グラフ内index, 緯度, 経度, 距離m)"""
        dd, k = self.kd.query([lat, lon])
        return (int(self.kd_idx[k]), float(self.kd_lat[k]), float(self.kd_lon[k]),
                float(dd * 111_000))

    def reachability(self, lat, lon, limit, cost='time', use_expressway=True, road_class='auto'):
        """到達圏。limit は API 単位（分 or m）。

        ジオメトリは返さない。道路の形は PMTiles で配信し、クライアントが link_id をキーに
        setFeatureState で着色する（設計書 1-1）。

        返り値 dict:
          link_ids / costs（同じ長さ・link_ids 昇順）, snap=(lat, lon, dist_m),
          road_class_applied, tier('ok'|'heavy'), counts={all, trunk, major}, elapsed_ms
        road_class を明示指定して LINKS_HARD_MAX を超えると TooManyLinks。
        """
        t0 = time.perf_counter()
        idx, slat, slon, sm = self.snap(lat, lon)
        G = self.graphs[(cost, use_expressway)]
        scale = COST_SCALE[cost]
        d = dijkstra(G, directed=True, indices=idx, limit=limit / scale)
        c = np.minimum(d[self.i1], d[self.i2])
        ok = np.isfinite(c)
        if not use_expressway:
            # 到達判定は「両端のどちらかが到達」なので、IC の両側が一般道で到達していると
            # 間の高速リンクが到達に見える。高速なしでは明示的に除く
            ok &= ~self.is_expressway

        counts = {k: int(np.count_nonzero(ok & m)) for k, m in self.class_mask.items()}
        if road_class == 'auto':
            applied = next((k for k in ROAD_CLASS_ORDER if counts[k] <= LINKS_HARD_MAX), 'major')
        else:
            applied = road_class
            if counts[applied] > LINKS_HARD_MAX:
                raise TooManyLinks(applied, counts)

        mask = ok & self.class_mask[applied]
        link_ids = np.flatnonzero(mask).astype(np.int64)      # ← 行番号 = link_id
        return {
            'link_ids': link_ids,
            'costs': c[mask] * scale,
            'snap': (slat, slon, sm),
            'road_class_applied': applied,
            'tier': 'ok' if len(link_ids) <= TIER_OK_MAX else 'heavy',
            'counts': counts,
            'elapsed_ms': (time.perf_counter() - t0) * 1000,
        }

    def component_name(self, idx):
        return self._component_name.get(int(self.component[idx]), '離島')

    def _links_for_path(self, path, cost, use_expressway):
        """ノード列 → リンク列。並行リンクは重みが最小のものを選ぶ（CSR に載っているのもそれ）"""
        u = np.asarray(path[:-1], dtype=np.int64)
        v = np.asarray(path[1:], dtype=np.int64)
        k = np.minimum(u, v) * self.n_nodes + np.maximum(u, v)
        lo = np.searchsorted(self._pk_sorted, k, 'left')
        hi = np.searchsorted(self._pk_sorted, k, 'right')
        w = self.link_time if cost == 'time' else self.link_dist
        out = np.empty(len(u), dtype=np.int64)
        for n, (l, h) in enumerate(zip(lo, hi)):
            cands = self._pk_order[l:h]
            if not use_expressway:
                c2 = cands[~self.is_expressway[cands]]
                cands = c2 if len(c2) else cands
            out[n] = cands[np.argmin(w[cands])]
        return out, u

    def route(self, from_lat, from_lon, to_lat, to_lon, cost='time', use_expressway=True):
        """2 地点間の最短経路。経路だけはジオメトリ（LineString 1 本）を返す（設計書 1-2）。

        返り値 dict: coordinates（[lon, lat] の list・None なら到達不能）, link_ids, summary,
        snap={origin_m, dest_m}, unreachable_reason, elapsed_ms
        """
        t0 = time.perf_counter()
        s, slat, slon, sm = self.snap(from_lat, from_lon)
        t, tlat, tlon, tm = self.snap(to_lat, to_lon)
        base = {'snap': {'origin_m': sm, 'dest_m': tm}, 'link_ids': np.empty(0, dtype=np.int64),
                'coordinates': None, 'summary': {'dist_m': 0, 'time_min': 0.0, 'link_count': 0}}

        def fail(reason):
            base.update(unreachable_reason=reason, elapsed_ms=(time.perf_counter() - t0) * 1000)
            return base

        # 別成分なら探索せずに即答（東京→札幌など）
        if self.component[s] != self.component[t]:
            return fail(f'始点と終点が別の道路網に属しています（{self.component_name(s)} / {self.component_name(t)}）')
        G = self.graphs[(cost, use_expressway)]
        # 打ち切りなしだと近くても全国（360 万ノード）を探し尽くし、1/2 vCPU で毎回 0.8 秒掛かる。
        # 直線距離から上限を見積もり、届かなければ倍にして探し直す。上限内のノードの距離は正確なので、
        # t に届いた時点の経路は打ち切りなしと同じ。最後は打ち切りなし（到達不能の判定もそこで行う）
        km = math.dist((slat, slon * math.cos(math.radians((slat + tlat) / 2))),
                       (tlat, tlon * math.cos(math.radians((slat + tlat) / 2)))) * 111
        guess = (km * ROUTE_MIN_PER_KM[use_expressway] + 15) if cost == 'time' else (km * 1500 + 2000)
        for limit in [guess * 2 ** k for k in range(ROUTE_LIMIT_TRIES)] + [np.inf]:
            d, pred = dijkstra(G, directed=True, indices=s, return_predecessors=True,
                               limit=limit / COST_SCALE[cost])
            if np.isfinite(d[t]):
                break
        if not np.isfinite(d[t]):
            return fail('高速道路を使わないと到達できません' if not use_expressway else '経路が見つかりません')

        path = [t]
        while path[-1] != s:
            path.append(pred[path[-1]])
        path.reverse()
        link_ids, from_nodes = self._links_for_path(path, cost, use_expressway)

        # 座標を繋ぐ。リンクの座標は node1 → node2 の向きなので、逆走なら反転。継ぎ目の重複点は落とす
        parts = []
        for n, lid in enumerate(link_ids):
            xy = self.coords[self.coord_offsets[lid]:self.coord_offsets[lid + 1]]
            if self.i1[lid] != from_nodes[n]:
                xy = xy[::-1]
            parts.append(xy if n == 0 else xy[1:])
        coords = np.concatenate(parts)

        base.update(
            coordinates=coords, link_ids=link_ids, unreachable_reason=None,
            summary={'dist_m': int(self.link_dist[link_ids].sum()),
                     'time_min': round(float(self.link_time[link_ids].sum()) * 0.01, 1),
                     'link_count': int(len(link_ids))},
            elapsed_ms=(time.perf_counter() - t0) * 1000)
        return base

    def health(self):
        return {
            'links': self.n_links, 'nodes': self.n_nodes, 'snap_nodes': self.n_snap_nodes,
            'components': int(self.n_components), 'coordinates': int(len(self.coords)),
            'load_seconds': round(self.load_seconds, 2), 'graph_source': self.source,
        }
