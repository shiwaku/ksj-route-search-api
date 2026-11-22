"""経路比較ビューワ（OSRM / GraphHopper / Valhalla）。同じ範囲・同じ速度のネットワークで、同じ始点・終点の経路が同じになるかを見る

    bash bench/compare/build.sh                                        # 範囲の切り出し・3 エンジンの前処理と起動（初回だけ）
    uv run uvicorn bench.compare.server:app --port 8010                # → http://localhost:8010/

始点・終点は KSJ の交差点ノードに寄せてから、その座標を 3 エンジンに渡す（エンジンごとの吸着の違いを消す）。
各エンジンの経路の形を KSJ の交差点ノードに照らしてリンク列に戻し、エンジンどうしで比べる。
「KSJ 換算」はリンク列に KSJ の所要（time_001min）を足し直した値。経路が違っても換算が同じなら、所要が同じ経路が複数あっただけ。
（RoadGraph はスナップとリンクの逆引きに使うだけで、scipy の探索結果とは比べない）
"""
import json
import math
import os
import random
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import shapely
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from scipy.spatial import KDTree

from api.graph import RoadGraph

CASE = os.environ.get('COMPARE_CASE', 'tokyo23')
OSRM = os.environ.get('OSRM_URL', 'http://localhost:5000')
GH = os.environ.get('GH_URL', 'http://localhost:8989')
VALHALLA = os.environ.get('VALHALLA_URL', 'http://localhost:8002')
HERE = Path(__file__).resolve().parent
MATCH_M = 1.5           # 経路の頂点を交差点ノードと見なす距離
LEN_TOL_M, LEN_TOL = 5, 0.03     # リンク長と経路の区間長の許容差（m・割合の大きい方）
# Valhalla は auto の既定のまま使う。50 組で試すと、ペナルティ系（maneuver_penalty など）を 0 にしても経路は変わらず、
# use_highways は既定の 0.5 が中立（1 で高速を好み、0.25 以下で避ける）。残るずれは Valhalla が内部で足す右左折のコストで、オプションでは消せない

g = RoadGraph(case=CASE, prebuilt=False)
# 全交差点ノードの座標（グラフの index 順）。RoadGraph はスナップ対象の分しか持たないので別に読む
def _node_lonlat():
    d = HERE.parents[1] / 'network' / CASE
    nodes = pq.read_table(d / f'KSJ_N13-24_{CASE}_道路ノード.parquet')
    links = pq.read_table(d / f'KSJ_N13-24_{CASE}_道路リンク.parquet', columns=['node1', 'node2']).to_pandas()
    uniq = np.unique(links.astype('int64').to_numpy().ravel())          # RoadGraph と同じ index の振り方
    nid = np.array(nodes['node_id'].to_pylist(), dtype=np.int64)
    xy = shapely.get_coordinates(shapely.from_wkb(nodes['geometry'].to_numpy(zero_copy_only=False)))
    out = np.full((len(uniq), 2), np.nan)
    m = np.isin(nid, uniq)
    out[np.searchsorted(uniq, nid[m])] = xy[m]
    return out


node_lonlat = _node_lonlat()
COS = math.cos(math.radians(float(np.nanmean(node_lonlat[:, 1]))))
node_kd = KDTree(np.column_stack([node_lonlat[:, 0] * COS, node_lonlat[:, 1]]))

app = FastAPI(title='経路比較ビューワ')
pool = ThreadPoolExecutor(8)


def _get(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                 headers={'Content-Type': 'application/json'} if body else {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {'_error': f'HTTP {e.code}: {e.read()[:300].decode(errors="replace")}'}
    except Exception as e:
        return {'_error': repr(e)}


def _decode_polyline6(s):
    out, lat, lon, i = [], 0, 0, 0
    while i < len(s):
        for k in range(2):
            shift = res = 0
            while True:
                b = ord(s[i]) - 63; i += 1
                res |= (b & 0x1f) << shift; shift += 5
                if b < 0x20:
                    break
            d = ~(res >> 1) if res & 1 else res >> 1
            if k == 0:
                lat += d
            else:
                lon += d
        out.append([lon / 1e6, lat / 1e6])
    return out


def osrm(a, b):
    r = _get(f'{OSRM}/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}?overview=full&geometries=geojson')
    if '_error' in r or r.get('code') != 'Ok':
        return {'error': r.get('_error') or r.get('message')}
    rt = r['routes'][0]
    return {'coords': rt['geometry']['coordinates'], 'dist_m': rt['distance'], 'time_min': rt['duration'] / 60}


def graphhopper(a, b):
    # way_point_max_distance=0: 既定（1 m）の Douglas-Peucker で直進する交差点の頂点が消え、リンク列に戻せなくなる。
    # 設定ファイルの routing.way_point_max_distance は効かなかった（12.0-SNAPSHOT）
    r = _get(f'{GH}/route?point={a[0]},{a[1]}&point={b[0]},{b[1]}&profile=car&points_encoded=false&instructions=false'
             '&way_point_max_distance=0')
    if '_error' in r or 'paths' not in r:
        return {'error': r.get('_error') or r.get('message')}
    p = r['paths'][0]
    return {'coords': p['points']['coordinates'], 'dist_m': p['distance'], 'time_min': p['time'] / 60000}


def valhalla(a, b):
    body = {'locations': [{'lat': a[0], 'lon': a[1]}, {'lat': b[0], 'lon': b[1]}],
            'costing': 'auto', 'directions_type': 'none', 'units': 'kilometers'}
    r = _get(f'{VALHALLA}/route', body)
    if '_error' in r or 'trip' not in r:
        return {'error': r.get('_error') or r.get('error')}
    leg = r['trip']['legs'][0]
    return {'coords': _decode_polyline6(leg['shape']), 'dist_m': leg['summary']['length'] * 1000,
            'time_min': leg['summary']['time'] / 60}


ENGINES = {'osrm': osrm, 'graphhopper': graphhopper, 'valhalla': valhalla}


def _link_between(u, v, seg_m):
    """ノード u, v を結ぶリンクのうち、長さが経路の区間長 seg_m に合うもの（並行リンクは長さの近い方）。無ければ None"""
    key = min(u, v) * g.n_nodes + max(u, v)
    lo, hi = np.searchsorted(g._pk_sorted, key, 'left'), np.searchsorted(g._pk_sorted, key, 'right')
    if lo == hi:
        return None
    cands = g._pk_order[lo:hi]
    err = np.abs(g.link_dist[cands] - seg_m)
    k = int(np.argmin(err))
    return int(cands[k]) if err[k] <= max(LEN_TOL_M, LEN_TOL * seg_m) else None


def to_links(coords):
    """経路の形 → KSJ のリンク列。繋げられなかった区間（照合の取りこぼし）は gaps に数える。

    頂点の近くの交差点ノードが、経路の通るノードとは限らない。
      - KSJ はメッシュ境界などで 3 m 以内に別のノードを置いている（23 区で 353 組・上下線で別のリンクにつながる）
      - 高架・地下のリンクは、下（上）の道路の交差点ノードの真上を通る。その交差点からの取り付け道路も実在するので、
        「リンクが在るか」だけで判定すると寄り道の並びを拾う
      - 首都高の高架の下を一般道が並走すると、一般道の短いリンクが高架の形に沿って連なる。
        「繋がった数」で数えると、長い高速のリンク 1 本より一般道の並び（途中で途切れる）が勝つ
    そこで頂点ごとに MATCH_M 以内の候補を全部挙げ、「候補を選ぶ／読み飛ばす」の並びのうち
    （繋がった延長 − 繋がらなかった延長）が最大のものを動的計画法で選ぶ。「繋がった」は、2 ノードを結ぶリンクがあり、
    その長さが経路の形に沿った区間長と合うこと。延長が同じならリンク数の少ない方（1 本 0.5 m の減点）"""
    xy = np.asarray(coords, dtype=np.float64)
    pts = np.column_stack([xy[:, 0] * COS, xy[:, 1]])
    cum = np.r_[0, np.cumsum(np.hypot(*np.diff(pts, axis=0).T))] * 111_000      # 頂点までの累積延長 m
    sets = []       # (頂点 index, 候補ノード)
    for i, c in enumerate(node_kd.query_ball_point(pts, MATCH_M / 111_000)):
        c = sorted(c)
        if c and (not sets or c != sets[-1][1]):
            sets.append((i, c))
    if not sets:
        return [], 0
    # 状態 = 最後に選んだノード → (点数 m, 頂点 index, 連結リスト (ノード, 直前からのリンク or None, 親))。
    # 読み飛ばしは状態をそのまま持ち越す
    i0, c0 = sets[0]
    states = {c: (0, i0, (c, None, None)) for c in c0}
    for i, cur in sets[1:]:
        nxt = dict(states)
        for c in cur:
            if c in states:         # 同じノードが続く（読み飛ばしと同じ）
                continue
            best = None
            for p, (sp, ip, path) in states.items():
                seg = cum[i] - cum[ip]
                lid = _link_between(p, c, seg)
                v = sp + (seg - 0.5 if lid is not None else -seg)
                if best is None or v > best[0]:
                    best = (v, i, (c, lid, path))
            nxt[c] = best
        # 同じ頂点の候補どうしを結ぶ短いリンク（メッシュ境界の 1 m のリンクなど）も渡れるようにする
        for _ in range(2):
            for c in cur:
                if c not in nxt or nxt[c][1] != i:
                    continue
                for c2 in cur:
                    lid = _link_between(c, c2, 0) if c2 != c else None
                    if lid is not None:
                        v = nxt[c][0] - 0.5
                        if c2 not in nxt or v > nxt[c2][0]:
                            nxt[c2] = (v, i, (c2, lid, nxt[c][2]))
        states = nxt
    _, _, path = max(states.values(), key=lambda s: s[0])
    links, gaps = [], 0
    while path and path[2] is not None:
        if path[1] is None:
            gaps += 1
        else:
            links.append(path[1])
        path = path[2]
    links.reverse()
    return links, gaps


PAIRS = [('osrm', 'graphhopper'), ('osrm', 'valhalla'), ('graphhopper', 'valhalla')]
TIE_MIN = 0.005         # KSJ 換算の所要の差がこれ未満なら「所要は同じ」（time_001min は 0.01 分単位）


def _pair(e1, e2):
    """2 エンジンの経路の比較。same = リンク列が完全一致、overlap = 共通リンクの延長 ÷ 和集合の延長"""
    if e1.get('error') or e2.get('error') or not e1['links'] or not e2['links']:
        return {'error': True}
    s1, s2 = set(e1['links']), set(e2['links'])
    union = list(s1 | s2)
    d = round(e2['ksj_time_min'] - e1['ksj_time_min'], 2)
    same = e1['links'] == e2['links']
    return {'same': same, 'tie': not same and abs(d) < TIE_MIN, 'ksj_time_diff_min': d,
            'overlap': round(float(g.link_dist[list(s1 & s2)].sum()) / float(g.link_dist[union].sum()), 4)}


def compare(a, b, with_geometry=True):
    """a, b = (lat, lon)。始点・終点を交差点ノードに寄せ、3 エンジンの経路を取ってリンク列で比べる"""
    s, slat, slon, _ = g.snap(*a)
    t, tlat, tlon, _ = g.snap(*b)
    A, B = (slat, slon), (tlat, tlon)
    futs = {name: pool.submit(f, A, B) for name, f in ENGINES.items()}
    res = {}
    for name, f in futs.items():
        e = f.result()
        if e.get('error'):
            res[name] = {'error': str(e['error']), 'coords': None, 'links': [], 'gaps': 0}
            continue
        e['links'], e['gaps'] = to_links(e['coords'])
        L = e['links']
        e['link_count'] = len(L)
        # KSJ 換算 = 経路のリンク列に KSJ の所要・延長を足し直した値。3 エンジンを同じ物差しで測る
        e['ksj_time_min'] = round(float(g.link_time[L].sum()) * 0.01, 2) if L else None
        e['ksj_dist_m'] = int(g.link_dist[L].sum()) if L else None
        res[name] = e
    pairs = {f'{p}|{q}': _pair(res[p], res[q]) for p, q in PAIRS}
    if not with_geometry:
        for e in res.values():
            e.pop('coords', None); e.pop('links', None)
    return {'origin': {'lat': slat, 'lon': slon, 'node': int(s)}, 'dest': {'lat': tlat, 'lon': tlon, 'node': int(t)},
            'engines': res, 'pairs': pairs}


@app.get('/')
def index():
    return FileResponse(HERE / 'index.html')


@app.get('/api/info')
def info():
    lon, lat = node_lonlat[:, 0], node_lonlat[:, 1]
    return {'case': CASE, 'links': g.n_links, 'nodes': g.n_nodes,
            'bbox': [float(np.nanmin(lon)), float(np.nanmin(lat)), float(np.nanmax(lon)), float(np.nanmax(lat))]}


@app.get('/api/compare')
def api_compare(from_lat: float, from_lon: float, to_lat: float, to_lon: float):
    return compare((from_lat, from_lon), (to_lat, to_lon))


@app.get('/api/batch')
def api_batch(n: int = Query(100, ge=1, le=1000), seed: int = 1):
    """ランダムな n 組で比べる。始点・終点はスナップ対象ノードから一様に選ぶ（同じ seed なら同じ組）"""
    rng = random.Random(seed)
    rows, t0 = [], time.perf_counter()
    for _ in range(n):
        i, j = rng.randrange(g.n_snap_nodes), rng.randrange(g.n_snap_nodes)
        a, b = (float(g.kd_lat[i]), float(g.kd_lon[i])), (float(g.kd_lat[j]), float(g.kd_lon[j]))
        c = compare(a, b, with_geometry=False)
        rows.append({'from': a, 'to': b, 'engines': c['engines'], 'pairs': c['pairs']})
    stats = {}
    for p, q in PAIRS:
        ok = [r['pairs'][f'{p}|{q}'] for r in rows if not r['pairs'][f'{p}|{q}'].get('error')]
        d = [abs(x['ksj_time_diff_min']) for x in ok if not x['same'] and not x['tie']]
        stats[f'{p}|{q}'] = {'pairs': len(ok), 'same': sum(x['same'] for x in ok), 'tie': sum(x['tie'] for x in ok),
                             'other': len(d), 'abs_diff_mean_min': round(float(np.mean(d)), 3) if d else 0,
                             'abs_diff_max_min': round(float(np.max(d)), 3) if d else 0,
                             'mean_overlap': round(float(np.mean([x['overlap'] for x in ok])), 4) if ok else None}
    all_same = sum(1 for r in rows if all(r['pairs'][f'{p}|{q}'].get('same') for p, q in PAIRS))
    gaps = {name: sum(r['engines'][name].get('gaps', 0) for r in rows) for name in ENGINES}
    return {'n': n, 'seed': seed, 'seconds': round(time.perf_counter() - t0, 1),
            'all_same': all_same, 'gaps': gaps, 'stats': stats, 'rows': rows}
