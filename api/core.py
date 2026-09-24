# -*- coding: utf-8 -*-
"""main.py と bookmarks.py が共有するもの: グラフの状態・到達圏レスポンスの組み立て"""

import time

import numpy as np
from fastapi import HTTPException

from api.graph import RoadGraph, TooManyLinks

LIMIT_MIN_MAX = 1920         # 本州・四国・九州の道路網の「直径」は佐多岬→大間崎 1,891 分。鹿児島起点は 1,800 分で全リンク（343 万本）に到達し以後増えない。80 分刻み × 24 帯 = 1,920 を上限に
LIMIT_DIST_MAX = 500_000     # cost=dist のときの上限（m）
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


def reachability_response(lat, lon, limit_min, cost='time', road_class='auto', use_expressway=True):
    """/reachability と /bookmarks/{id}/reachability で同じ形を返す（設計書 3 章）"""
    check_japan(lat, lon)
    hard = LIMIT_MIN_MAX if cost == 'time' else LIMIT_DIST_MAX
    if limit_min > hard:
        raise HTTPException(422, f'limit_min は {hard:,} 以下にしてください（cost={cost}）')
    g = graph()
    try:
        r = g.reachability(lat, lon, limit_min, cost=cost, use_expressway=use_expressway, road_class=road_class)
    except TooManyLinks as e:
        raise HTTPException(400, str(e))
    slat, slon, sm = r['snap']
    return {
        'origin': {'lat': lat, 'lon': lon},
        'snap': {'lat': slat, 'lon': slon, 'dist_m': round(sm, 1)},
        'limit_min': limit_min, 'cost': cost,
        'road_class': road_class, 'use_expressway': use_expressway,
        'road_class_applied': r['road_class_applied'], 'tier': r['tier'],
        'counts': r['counts'], 'count': int(len(r['link_ids'])),
        'elapsed_ms': round(r['elapsed_ms'], 1),
        'link_ids': r['link_ids'].tolist(),
        'costs': np.round(r['costs'], 2).tolist(),
    }
