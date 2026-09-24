# -*- coding: utf-8 -*-
"""ブックマーク CRUD（設計書 4 章）"""

from typing import Optional

import psycopg
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from api import db
from api.core import LIMIT_MIN_MAX, check_japan, reachability_response

router = APIRouter(prefix='/bookmarks', tags=['bookmarks'])


class BookmarkIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    memo: Optional[str] = None
    lat: float
    lon: float
    limit_min: int = Field(120, gt=0, le=LIMIT_MIN_MAX)
    use_expressway: bool = True


class BookmarkPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    memo: Optional[str] = None
    limit_min: Optional[int] = Field(None, gt=0, le=LIMIT_MIN_MAX)
    use_expressway: Optional[bool] = None


# 1 行 → GeoJSON Feature。一覧はそのまま MapLibre の source に渡せる
COLS = 'id, name, memo, ST_X(geom) AS lon, ST_Y(geom) AS lat, limit_min, use_expressway, created_at, updated_at'


def feature(row):
    return {
        'type': 'Feature', 'id': row['id'],
        'geometry': {'type': 'Point', 'coordinates': [row['lon'], row['lat']]},
        'properties': {k: (row[k].isoformat() if k.endswith('_at') else row[k])
                       for k in ('name', 'memo', 'limit_min', 'use_expressway', 'created_at', 'updated_at')},
    }


def conn():
    try:
        return db.connect()
    except psycopg.OperationalError as e:
        raise HTTPException(503, f'データベースに接続できません: {e}')


def fetch_or_404(c, id_):
    row = c.execute(f'SELECT {COLS} FROM bookmarks WHERE id = %s', (id_,)).fetchone()
    if row is None:
        raise HTTPException(404, 'ブックマークが見つかりません')
    return row


@router.post('', status_code=201)
def create(b: BookmarkIn):
    check_japan(b.lat, b.lon)
    with conn() as c:
        row = c.execute(
            f'''INSERT INTO bookmarks (name, memo, geom, limit_min, use_expressway)
                VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s)
                RETURNING {COLS}''',
            (b.name, b.memo, b.lon, b.lat, b.limit_min, b.use_expressway)).fetchone()
    return feature(row)


@router.get('')
def list_():
    with conn() as c:
        rows = c.execute(f'SELECT {COLS} FROM bookmarks ORDER BY created_at DESC').fetchall()
    return {'type': 'FeatureCollection', 'features': [feature(r) for r in rows]}


@router.get('/{id_}')
def get(id_: int):
    with conn() as c:
        return feature(fetch_or_404(c, id_))


@router.patch('/{id_}')
def patch(id_: int, p: BookmarkPatch):
    with conn() as c:
        fetch_or_404(c, id_)
        row = c.execute(
            f'''UPDATE bookmarks SET
                  name = COALESCE(%s, name), memo = COALESCE(%s, memo),
                  limit_min = COALESCE(%s, limit_min), use_expressway = COALESCE(%s, use_expressway),
                  updated_at = now()
                WHERE id = %s RETURNING {COLS}''',
            (p.name, p.memo, p.limit_min, p.use_expressway, id_)).fetchone()
    return feature(row)


@router.delete('/{id_}', status_code=204)
def delete(id_: int):
    with conn() as c:
        fetch_or_404(c, id_)
        c.execute('DELETE FROM bookmarks WHERE id = %s', (id_,))
    return Response(status_code=204)


@router.get('/{id_}/reachability')
def reachability(id_: int):
    """保存地点から到達圏を再実行（CRUD と目玉を繋ぐ）。/reachability と同じ形"""
    with conn() as c:
        row = fetch_or_404(c, id_)
    return reachability_response(row['lat'], row['lon'], row['limit_min'], use_expressway=row['use_expressway'])
