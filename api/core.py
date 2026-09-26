# -*- coding: utf-8 -*-
"""main.py と bookmarks.py が共有するもの: グラフの状態・到達圏レスポンスの組み立て"""

import struct
import threading
import time

import numpy as np
import orjson
from fastapi import HTTPException
from fastapi.responses import Response

from api.graph import RoadGraph, TooManyLinks

LIMIT_MIN_MAX = 1920         # 本州・四国・九州の道路網の「直径」は佐多岬→大間崎 1,891 分。鹿児島起点は 1,800 分で全リンク（343 万本）に到達し以後増えない。80 分刻み × 24 帯 = 1,920 を上限に
LIMIT_DIST_MAX = 500_000     # cost=dist のときの上限（m）
# 到達圏を同時に計算する本数。1 本ごとに結果の配列を持つので、重なるとメモリが溢れる
# （standard-1 相当 4 GiB で 120 分 × 8 本同時に OOM・deploy-cloudflare.md 8 章）。超えた分はスレッドで待たせる
REACH_CONCURRENCY = 2
_reach_slots = threading.BoundedSemaphore(REACH_CONCURRENCY)
LAT_RANGE, LON_RANGE = (20.0, 46.0), (122.0, 154.0)

state = {'graph': None, 'started_at': time.time(), 'error': None, 'db_error': None}


def graph() -> RoadGraph:
    g = state['graph']
    if g is None:
        if state['error']:
            raise HTTPException(500, f'グラフの読み込みに失敗しました: {state["error"]}')
        raise HTTPException(503, 'グラフを読み込み中です')
    return g


def check_japan(lat, lon):
    if not (LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]):
        raise HTTPException(400, '緯度経度が日本の範囲外です')


# format=bin のコストの単位（16 ビット整数に丸める）。時間は 0.1 分（上限 1,920 分 → 19,200）、距離は 10 m（上限 500 km → 50,000）
BIN_COST_UNIT = {'time': 0.1, 'dist': 10.0}


def _binary_body(meta, link_ids, costs, cost):
    """到達圏のバイナリ形式（issue #5）。JSON の配列は 120 分で gzip 後 3.6 MB あり、公開版では転送に 1 秒掛かっていた。

    [u32 ヘッダ長 H][ヘッダ JSON（H バイト・末尾は 4 の倍数まで空白で埋める）][link_id の差分 u32 × count][コスト u16 × count]
    すべてリトルエンディアン。link_id は昇順なので差分は小さく、gzip がよく効く（120 分で 0.95 MB）。
    コスト = u16 × ヘッダの cost_unit
    """
    unit = BIN_COST_UNIT[cost]
    head = orjson.dumps({**meta, 'cost_unit': unit, 'encoding': 'delta-u32+u16'}, option=orjson.OPT_SERIALIZE_NUMPY)
    head += b' ' * (-(4 + len(head)) % 4)
    deltas = np.diff(link_ids, prepend=0).astype('<u4')
    q = np.minimum(np.round(costs / unit), 65535).astype('<u2')
    return struct.pack('<I', len(head)) + head + deltas.tobytes() + q.tobytes()


def reachability_response(lat, lon, limit_min, cost='time', road_class='auto', use_expressway=True, fmt='json'):
    """/reachability と /bookmarks/{id}/reachability で同じ形を返す（設計書 3 章）。fmt='bin' はバイナリ（_binary_body）"""
    check_japan(lat, lon)
    hard = LIMIT_MIN_MAX if cost == 'time' else LIMIT_DIST_MAX
    if limit_min > hard:
        raise HTTPException(422, f'limit_min は {hard:,} 以下にしてください（cost={cost}）')
    g = graph()
    with _reach_slots:
        try:
            r = g.reachability(lat, lon, limit_min, cost=cost, use_expressway=use_expressway, road_class=road_class)
        except TooManyLinks as e:
            raise HTTPException(400, str(e))
        slat, slon, sm = r['snap']
        meta = {
            'origin': {'lat': lat, 'lon': lon},
            'snap': {'lat': slat, 'lon': slon, 'dist_m': round(sm, 1)},
            'limit_min': limit_min, 'cost': cost,
            'road_class': road_class, 'use_expressway': use_expressway,
            'road_class_applied': r['road_class_applied'], 'tier': r['tier'],
            'counts': r['counts'], 'count': int(len(r['link_ids'])),
            'elapsed_ms': round(r['elapsed_ms'], 1),
        }
        if fmt == 'bin':
            return Response(_binary_body(meta, r['link_ids'], r['costs'], cost), media_type='application/octet-stream')
        body = {
            **meta,
            # 配列は numpy のまま orjson に渡す。dict を返して FastAPI の jsonable_encoder に任せると
            # 120 分（78 万リンク）で 3.8 秒・大量の Python オブジェクトを作る。orjson 直なら 0.08 秒（1/2 vCPU）
            'link_ids': r['link_ids'],
            'costs': np.round(r['costs'].astype(np.float64), 2),   # float32 のまま出すと 12.350000381 のような値になる
        }
        return Response(orjson.dumps(body, option=orjson.OPT_SERIALIZE_NUMPY), media_type='application/json')
