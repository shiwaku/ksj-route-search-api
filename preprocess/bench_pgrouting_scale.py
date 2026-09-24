#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pgRouting を規模別に実測する（bench_scale.py の pgRouting 版）

規模の比較にジオメトリは要らないので `id/source/target/cost/reverse_cost` だけ投入する。
（geom も入れると全国で 2,354 MB になる）

  python3 bench_pgrouting_scale.py --cases t1 t4 t12 t30 t70 nationwide
"""

import argparse
import re
import subprocess
import time
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
from scipy.spatial import KDTree

BASE = Path(__file__).resolve().parent.parent
CONTAINER = 'postgis'   # kartoza/postgis を docker run --name postgis で立てたもの
LIMIT = 3000        # 30 分（0.01 分単位）


def psql(sql, timing=False):
    cmd = ['docker', 'exec', '-i', '-e', 'PGPASSWORD=docker', CONTAINER,
           'psql', '-h', '127.0.0.1', '-U', 'docker', '-d', 'docker', '-v', 'ON_ERROR_STOP=1']
    if timing:
        sql = '\\timing on\n' + sql
    r = subprocess.run(cmd, input=sql, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f'psql 失敗:\n{r.stderr[:500]}')
    return r.stdout


def copy_csv(table, path):
    cmd = ['docker', 'exec', '-i', '-e', 'PGPASSWORD=docker', CONTAINER,
           'psql', '-h', '127.0.0.1', '-U', 'docker', '-d', 'docker', '-v', 'ON_ERROR_STOP=1',
           '-c', f"\\copy {table}(id,source,target,cost,reverse_cost) FROM STDIN CSV HEADER"]
    with open(path, 'rb') as f:
        r = subprocess.run(cmd, stdin=f, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f'COPY 失敗:\n{r.stderr[:500]}')


ap = argparse.ArgumentParser()
ap.add_argument('--cases', nargs='+', required=True)
ap.add_argument('--lat', type=float, default=35.681236)
ap.add_argument('--lon', type=float, default=139.767125)
ap.add_argument('--repeat', type=int, default=3)
ap.add_argument('--tmp', default='/tmp')
a = ap.parse_args()

print(psql('SELECT extversion FROM pg_extension WHERE extname=\'pgrouting\';').strip())

rows = []
for case in a.cases:
    d = BASE / 'network' / case
    links = pd.read_parquet(d / f'KSJ_N13-24_{case}_道路リンク.parquet',
                            columns=['node1', 'node2', 'time_001min'])
    nodes = gpd.read_parquet(d / f'KSJ_N13-24_{case}_道路ノード.parquet')
    n1 = links['node1'].astype('int64').to_numpy()
    n2 = links['node2'].astype('int64').to_numpy()
    w = links['time_001min'].astype('float64').to_numpy()
    uniq = np.unique(np.concatenate([n1, n2]))

    lat = nodes.geometry.y.to_numpy(); lon = nodes.geometry.x.to_numpy()
    _, k = KDTree(np.column_stack([lat, lon])).query([a.lat, a.lon])
    o = int(np.searchsorted(uniq, nodes['node_id'].astype('int64').to_numpy()[k]))

    df = pd.DataFrame({
        'id': np.arange(len(n1), dtype=np.int64),
        'source': np.searchsorted(uniq, n1),
        'target': np.searchsorted(uniq, n2),
        'cost': w, 'reverse_cost': w,
    })
    csv = Path(a.tmp) / f'bench_{case}.csv'
    t0 = time.perf_counter(); df.to_csv(csv, index=False); csv_s = time.perf_counter() - t0

    tbl = f'bench_{case}'
    psql(f'DROP TABLE IF EXISTS {tbl}; CREATE TABLE {tbl}('
         f'id bigint PRIMARY KEY, source bigint, target bigint, '
         f'cost double precision, reverse_cost double precision);')
    t0 = time.perf_counter(); copy_csv(tbl, csv); copy_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    psql(f'CREATE INDEX ON {tbl}(source); CREATE INDEX ON {tbl}(target); ANALYZE {tbl};')
    idx_s = time.perf_counter() - t0

    q = (f"SELECT count(*) FROM pgr_drivingDistance("
         f"'SELECT id, source, target, cost, reverse_cost FROM {tbl}', {o}, {LIMIT});")
    ts, cnt = [], None
    for _ in range(a.repeat):
        out = psql(q, timing=True)
        ts.append(float(re.search(r'Time: ([\d.]+) ms', out).group(1)))
        cnt = int(re.search(r'\n\s*(\d+)\n', out).group(1))
    size = psql(f"SELECT pg_size_pretty(pg_total_relation_size('{tbl}'));").split('\n')[2].strip()

    rows.append(dict(case=case, links=len(n1), csv_s=csv_s, copy_s=copy_s, idx_s=idx_s,
                     ms=float(np.median(ts)), reached=cnt, size=size))
    print(f'  {case:<12}{len(n1):>10,} リンク  COPY {copy_s:>5.1f}s  索引 {idx_s:>5.1f}s  '
          f'到達圏 {np.median(ts):>8.0f} ms  到達 {cnt:>9,}  {size}', flush=True)
    csv.unlink(missing_ok=True)

print('\n===== pgRouting: 規模 vs 到達圏30分 =====')
h = f'{"ケース":<12}{"リンク":>11}{"投入(COPY+索引)":>18}{"到達圏":>11}{"到達ノード":>12}{"テーブル":>11}'
print(h); print('-' * 78)
for r in rows:
    print(f'{r["case"]:<12}{r["links"]:>11,}{r["copy_s"]+r["idx_s"]:>16.1f}s'
          f'{r["ms"]:>9.0f}ms{r["reached"]:>12,}{r["size"]:>11}')
