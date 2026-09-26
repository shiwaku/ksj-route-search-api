# -*- coding: utf-8 -*-
"""PostgreSQL / PostGIS 接続（psycopg3 + 生 SQL・設計書 ADR-5）

ORM を使わない理由: Prisma は PostGIS の geometry 型に非対応、CRUD 5 本に SQLAlchemy は過剰。
ST_* を素直に書けるのが利点。スキーマは起動時にここで作る（compose 側に初期化 SQL を持たない）。
"""

import os

import psycopg
from psycopg.rows import dict_row

# 未設定 = DB なし（Cloudflare 公開版・deploy-cloudflare.md 5 章）。ブックマークと /bench を登録せず、/health も接続しない。
# ローカルは compose が設定する。ホストで uvicorn を動かすときは DATABASE_URL=postgresql://route:route@localhost:5433/route
DSN = os.environ.get('DATABASE_URL')

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE IF NOT EXISTS bookmarks (
  id             serial PRIMARY KEY,
  name           text NOT NULL,
  memo           text,
  geom           geometry(Point, 4326) NOT NULL,
  limit_min      integer NOT NULL DEFAULT 120,        -- 保存時の探索条件も一緒に持つ
  use_expressway boolean NOT NULL DEFAULT true,       -- 高速あり / なし
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bookmarks_geom_idx ON bookmarks USING gist (geom);
"""


def connect():
    return psycopg.connect(DSN, row_factory=dict_row, connect_timeout=3)


def init_schema():
    with connect() as conn:
        conn.execute(SCHEMA)
