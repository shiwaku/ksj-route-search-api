# -*- coding: utf-8 -*-
"""GET /bench — 比較用。同じ起点・同じ打ち切りで scipy（in-memory）と pgRouting を叩いて比べる（設計書 3 章・ADR-1）

本番の探索には使わない。ADR-1 の実測を、その場で再現して見せるためのもの。

比較が成り立つ根拠: ノード番号の付け方が同じ
  api/graph.py も preprocess/export_for_pgrouting.py も、node1/node2 を np.unique で並べた
  0 始まりの連番に振り直している。だから scipy のノード index を、そのまま pgRouting の
  source/target（roads_jp）に渡せる。

roads_jp の投入（DB を作り直したら再実行・4 秒）:
  cut -d, -f1-5 network/nationwide/pgrouting_nationwide.csv \\
    | docker compose exec -T -e PGPASSWORD=route db psql -h localhost -U route -d route \\
        -c "\\copy roads_jp FROM STDIN CSV HEADER"
  （事前に CREATE TABLE roads_jp(id bigint PRIMARY KEY, source bigint, target bigint,
    cost double precision, reverse_cost double precision) と source/target の btree）
"""

import math
import time

import numpy as np
from fastapi import APIRouter, Query
from scipy.sparse.csgraph import dijkstra

from api import db
from api.core import LIMIT_MIN_MAX, check_japan, graph
from api.graph import COST_SCALE

router = APIRouter(tags=['bench'])

PGR_SQL = 'SELECT id, source, target, cost, reverse_cost FROM roads_jp'
# 実務での pgRouting の使い方: エッジを取る SQL に bbox を掛け、全 388 万行を読まずに済ませる。
# 到達圏は結果の範囲が先に分からないので、最高速度 80 km/h × 打ち切り時間 を半径にした安全側の矩形で絞る
MAX_SPEED_KMH = 80


def _bbox_sql(lat, lon, limit_min):
    r_km = MAX_SPEED_KMH * limit_min / 60
    dlat = r_km / 111.0
    dlon = r_km / (111.0 * max(math.cos(math.radians(lat)), 0.2))
    return (PGR_SQL + ' WHERE geom && ST_MakeEnvelope(%f, %f, %f, %f, 4326)'
            % (lon - dlon, lat - dlat, lon + dlon, lat + dlat)), r_km


def _scipy(g, idx, limit):
    # ① in-memory: CSR に Dijkstra。limit で早期に打ち切る（到達圏が速い理由そのもの）
    t0 = time.perf_counter()
    d = dijkstra(g.graphs[('time', True)], directed=True, indices=idx, limit=limit)
    nodes = np.flatnonzero(np.isfinite(d))
    return nodes, (time.perf_counter() - t0) * 1000


def _pgrouting(idx, limit, verify, edge_sql=PGR_SQL):
    # ② pgRouting: 毎クエリ SQL でエッジを読んでグラフを組む。全行（388 万）だとここが 3 秒の正体
    with db.connect() as c:
        t0 = time.perf_counter()
        n = c.execute(
            'SELECT count(*) AS n FROM pgr_drivingDistance(%s, %s, %s)',
            (edge_sql, int(idx), float(limit))).fetchone()['n']
        ms = (time.perf_counter() - t0) * 1000
        nodes = None
        if verify:
            # ③ 結果が同じかは件数ではなくノード集合で見る（もう 1 回読むので時間は倍かかる）
            rows = c.execute(
                'SELECT node FROM pgr_drivingDistance(%s, %s, %s)',
                (edge_sql, int(idx), float(limit))).fetchall()
            nodes = np.fromiter((r['node'] for r in rows), dtype=np.int64, count=len(rows))
    return n, ms, nodes


@router.get('/bench')
def bench(
    lat: float = Query(35.6812, description='既定は東京駅'),
    lon: float = Query(139.7671),
    limit_min: float = Query(30, gt=0, le=LIMIT_MIN_MAX),
    verify: bool = Query(True, description='ノード集合の一致まで確かめる（pgRouting をもう 1 回叩く）'),
    bbox: bool = Query(False, description='pgRouting のエッジ SQL を bbox（80 km/h × 打ち切り）で絞る。実務寄りの使い方で比べる'),
):
    """scipy vs pgRouting。同じ起点・同じ打ち切りで到達ノード数と所要時間を並べる。本番探索には使わない"""
    check_japan(lat, lon)
    g = graph()
    idx, slat, slon, sm = g.snap(lat, lon)
    limit = limit_min / COST_SCALE['time']           # 分 → 0.01 分（両者ともコスト列は time_001min）

    s_nodes, s_ms = _scipy(g, idx, limit)
    body = {
        'origin': {'lat': lat, 'lon': lon},
        'snap': {'lat': slat, 'lon': slon, 'dist_m': round(sm, 1), 'node': idx},
        'limit_min': limit_min,
        'scipy': {'elapsed_ms': round(s_ms, 1), 'count': int(len(s_nodes))},
        'pgrouting': None, 'identical': None, 'ratio': None,
    }
    edge_sql, r_km = (_bbox_sql(lat, lon, limit_min) if bbox else (PGR_SQL, None))
    body['pgrouting_edges'] = 'bbox 半径 %.0f km' % r_km if bbox else '全 388 万行'
    try:
        p_n, p_ms, p_nodes = _pgrouting(idx, limit, verify, edge_sql)
    except Exception as e:
        # ④ DB が落ちていても scipy 側は返す（設計書 7 章 ④ の決定）。理由は見えるようにする
        body['pgrouting'] = {'error': repr(e)}
        return body
    body['pgrouting'] = {'elapsed_ms': round(p_ms, 1), 'count': int(p_n)}
    body['ratio'] = round(p_ms / s_ms, 1) if s_ms > 0 else None
    if p_nodes is not None:
        body['identical'] = bool(len(p_nodes) == len(s_nodes)
                                 and np.array_equal(np.sort(p_nodes), s_nodes))
    return body
