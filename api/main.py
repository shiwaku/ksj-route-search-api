#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Route Search API（FastAPI）— 全国道路ネットワークの到達圏分析・経路探索

仕様は docs/api-design.md（v0.4）。

  uv run uvicorn api.main:app --port 8000
"""

import asyncio
import ctypes
import gc
import os
import time
from contextlib import asynccontextmanager
from typing import Literal

import numpy as np
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from api import db
from api.bench import router as bench_router
from api.bookmarks import router as bookmarks_router
from api.core import (WALK_CASE, WALK_KMH_DEFAULT, WALK_LIMIT_MIN_MAX, check_japan, check_walk_area, graph,
                      reachability_response, state, walk_m_per_min)
from api.graph import RoadGraph


@asynccontextmanager
async def lifespan(app: FastAPI):
    # グラフ構築（全国 約 5 秒）はスレッドで回し、完了までは探索系が 503 を返す（設計書 2-4）
    loop = asyncio.get_running_loop()

    def load():
        try:
            state['graph'] = RoadGraph()
        except Exception as e:      # 起動失敗を /health で見えるようにする
            state['error'] = repr(e)
        # 歩行者（北海道・issue #55）は車の後に読む。車は先に使えるようになり、歩行者は読み終わるまで 503。
        # 失敗しても車は止めない
        if WALK_CASE:
            try:
                state['walk_graph'] = RoadGraph(WALK_CASE)
            except Exception as e:
                state['walk_error'] = repr(e)
        # 読み込み中の一時オブジェクト（文字列の node_id・WKB など）を解放しても glibc は OS に返さない。
        # 返さないと常駐が 3.26 GB のまま、返すと 2.26 GB（standard-1 は 4 GiB・deploy-cloudflare.md 8 章）
        gc.collect()
        try:
            ctypes.CDLL('libc.so.6').malloc_trim(0)
        except OSError:             # glibc 以外（macOS でのローカル実行）では何もしない
            pass

    def init_db():
        # DB が落ちていても探索系は動かす。/bookmarks が 503 を返し、/health に理由が出る
        try:
            db.init_schema()
            state['db_error'] = None
        except Exception as e:
            state['db_error'] = repr(e)
    loop.run_in_executor(None, load)
    if db.DSN:
        loop.run_in_executor(None, init_db)
    yield


app = FastAPI(title='Route Search API', version='0.4', lifespan=lifespan,
              description='全国道路ネットワークの到達圏分析・経路探索。仕様は docs/api-design.md')
# 到達圏レスポンスは 30 分で 2 MB・120 分で 12 MB。gzip は必須だが lv9 だと 120 分で 1.8 秒掛かる
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=1)
app.add_middleware(CORSMiddleware, allow_origins=['http://localhost:5173'],
                   allow_methods=['*'], allow_headers=['*'])
# DB 系は DATABASE_URL があるときだけ。無ければルーターごと登録しない（404）。画面は /health の features で出し分ける
FEATURES = ['bookmarks', 'bench'] if db.DSN else []
if db.DSN:
    app.include_router(bookmarks_router)
    app.include_router(bench_router)


@app.get('/health')
def health():
    g = state['graph']
    body = {'status': 'ok' if g else ('error' if state['error'] else 'loading'),
            'graph_loaded': g is not None,
            'uptime_seconds': round(time.time() - state['started_at'], 1),
            'features': FEATURES}
    # 公開版（Cloudflare Containers）では、コンテナが動いている拠点が入る（例: bom03 = ムンバイ）。
    # 拠点は「リクエスト元に一番近い、イメージを取得済みの拠点」で決まり、遠いと Worker との往復が遅い（2026-09-26）
    if os.environ.get('CLOUDFLARE_LOCATION'):
        body['location'] = os.environ['CLOUDFLARE_LOCATION']
        body['region'] = os.environ.get('CLOUDFLARE_REGION')
    if g:
        body.update(g.health())
    if state['error']:
        body['error'] = state['error']
    # 歩行者のグラフ（無効なら enabled=false）。画面は loaded を見て徒歩の切り替えを出す
    wg = state['walk_graph']
    body['walk'] = {'enabled': bool(WALK_CASE), 'case': WALK_CASE or None, 'loaded': wg is not None,
                    'limit_min_max': WALK_LIMIT_MIN_MAX, 'walk_kmh_default': WALK_KMH_DEFAULT}
    if wg:
        body['walk'].update(wg.health())
    if state['walk_error']:
        body['walk']['error'] = state['walk_error']
    if not db.DSN:      # DB なしでは接続を試みない（試みると connect_timeout=3 で毎回 3 秒待つ）
        return body
    # DB は起動時の結果ではなく毎回つなぎ直して見る。起動時に落ちていて後から上がる（9/10 に踏んだ）と
    # /bookmarks は動くのに /health だけ error のまま、という食い違いが起きるため
    try:
        with db.connect() as c:
            c.execute('SELECT 1')
        body['db'] = 'ok'
    except Exception as e:
        body['db'] = f'error: {e!r}'
    return body


@app.get('/reachability')
def reachability(
    lat: float, lon: float,
    limit_min: float = Query(120, gt=0, description='打ち切り。分（cost=dist のときはメートル）'),
    cost: Literal['time', 'dist'] = 'time',
    road_class: Literal['auto', 'all', 'trunk', 'major'] = 'auto',
    use_expressway: bool = True,
    format: Literal['json', 'bin'] = Query('json', description='bin は配列をバイナリで返す（画面用。形式は api/core.py の _binary_body）'),
    mode: Literal['vehicle', 'walk'] = Query('vehicle', description='walk は歩行者（北海道だけ・limit_min は 120 分まで）。cost・road_class・use_expressway は無視'),
    walk_kmh: float = Query(WALK_KMH_DEFAULT, ge=1.0, le=10.0, description='歩く速さ km/h（mode=walk のときだけ）'),
):
    """到達圏。ジオメトリは返さず link_id とコストの並列配列を返す（クライアントは PMTiles に setFeatureState）"""
    return reachability_response(lat, lon, limit_min, cost=cost, road_class=road_class, use_expressway=use_expressway,
                                 fmt=format, mode=mode, walk_kmh=walk_kmh)


@app.get('/route')
def route(
    from_lat: float, from_lon: float, to_lat: float, to_lon: float,
    cost: Literal['time', 'dist'] = 'time',
    use_expressway: bool = True,
    mode: Literal['vehicle', 'walk'] = Query('vehicle', description='walk は歩行者（北海道だけ・120 分まで）。cost・use_expressway は無視'),
    walk_kmh: float = Query(WALK_KMH_DEFAULT, ge=1.0, le=10.0, description='歩く速さ km/h（mode=walk のときだけ）'),
):
    """2 地点間の最短経路。経路だけは LineString を返す。到達不能は 200 で geometry=null"""
    check_japan(from_lat, from_lon)
    check_japan(to_lat, to_lon)
    if mode == 'walk':
        # 歩行者は距離で探し、所要時間を walk_kmh で出し直す（グラフの time_001min は 4.0 km/h 固定のため）
        g = graph('walk')
        check_walk_area(g, from_lat, from_lon)
        check_walk_area(g, to_lat, to_lon)
        mpm = walk_m_per_min(walk_kmh)
        cap = WALK_LIMIT_MIN_MAX * mpm
        cost, use_expressway = 'time', True
        r = g.route(from_lat, from_lon, to_lat, to_lon, cost='dist', limit_cap=cap,
                    cap_reason=f'徒歩 {WALK_LIMIT_MIN_MAX} 分（{walk_kmh:g} km/h で {cap / 1000:.1f} km）では届きません')
        r['summary']['time_min'] = round(r['summary']['dist_m'] / mpm, 1)
    else:
        r = graph().route(from_lat, from_lon, to_lat, to_lon, cost=cost, use_expressway=use_expressway)
    geom = None
    if r['coordinates'] is not None:
        # 座標は float32 で常駐（約 1 m 精度）。float64 に上げてから 5 桁に丸めないと
        # 139.76596069335938 のような float32 の生値が JSON に出る
        geom = {'type': 'LineString',
                'coordinates': np.round(r['coordinates'].astype(np.float64), 5).tolist()}
    return {
        'geometry': geom,
        'summary': r['summary'],
        'snap': {k: round(v, 1) for k, v in r['snap'].items()},
        'cost': cost, 'use_expressway': use_expressway, 'mode': mode,
        **({'walk_kmh': walk_kmh} if mode == 'walk' else {}),
        'unreachable_reason': r['unreachable_reason'],
        'elapsed_ms': round(r['elapsed_ms'], 1),
    }
