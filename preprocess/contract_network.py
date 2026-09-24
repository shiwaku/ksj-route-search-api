#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
道路ネットワークの縮約（交差点間リンク化）

ksj_to_network_csv.py が出力した道路リンク/ノード parquet を読み、
**次数2のノード（交差点ではない単なる通過点）を畳んで、交差点間で1リンク**にする。
DRM（デジタル道路地図）の基本道路網に近い構造にするための後処理。

【なぜやるか】
N13 の道路中心線は属性変化やメッシュ境界で細分されており、
端点ノードの約 80% が次数2（通過点）。畳むとリンク数・ノード数が大幅に減り、
Dijkstra の計算量（ノード数・エッジ数に比例）が下がる。

【保存されるもの】
dist_m と time_001min は **加算で完全に保存される**。
つまり縮約しても距離・所要時間は変わらない（速度テーブルを残す構成とも両立する）。

【失われるもの】
リンク単位の道路種別（N13_003 等）。1本の縮約リンクが複数種別をまたぐ場合があるため。
→ 描画は元リンクで行うので、対応表（縮約リンク → 元 link_id）を別途出力する。

【使い方】
  python3 contract_network.py --case 6441_drm
  python3 contract_network.py --case nationwide --net-dir network
"""

import argparse
import time
from pathlib import Path

import numpy as np
import geopandas as gpd
from shapely.geometry import LineString

parser = argparse.ArgumentParser()
parser.add_argument('--case', required=True, help='ケース名（network/{case}/ を読む）')
parser.add_argument('--net-dir', default='network', help='ネットワークデータのルート')
parser.add_argument('--suffix', default='_contracted', help='出力ケース名の接尾辞')
args = parser.parse_args()

BASE = Path(__file__).resolve().parent.parent
IN_DIR = BASE / args.net_dir / args.case
OUT_DIR = BASE / args.net_dir / f'{args.case}{args.suffix}'
OUT_DIR.mkdir(parents=True, exist_ok=True)

LINKS_IN = IN_DIR / f'KSJ_N13-24_{args.case}_道路リンク.parquet'
NODES_IN = IN_DIR / f'KSJ_N13-24_{args.case}_道路ノード.parquet'
CASE_OUT = f'{args.case}{args.suffix}'

t0 = time.time()


def el(msg):
    print(f'[{time.time() - t0:6.1f}s] {msg}', flush=True)


# ─────────────────────────────────────────────────────────
# 読み込み
# ─────────────────────────────────────────────────────────
el(f'読み込み: {LINKS_IN.name}')
links = gpd.read_parquet(LINKS_IN).reset_index(drop=True)
nodes = gpd.read_parquet(NODES_IN)
L = len(links)
el(f'  リンク {L:,} 本 / ノード {len(nodes):,} 件')

n1 = links['node1'].astype('int64').to_numpy()
n2 = links['node2'].astype('int64').to_numpy()

# ノードID → 連番インデックス（dict を使わず searchsorted で引く）
uniq = np.unique(np.concatenate([n1, n2]))
NN = len(uniq)
i1 = np.searchsorted(uniq, n1)
i2 = np.searchsorted(uniq, n2)

# ─────────────────────────────────────────────────────────
# 次数と隣接リスト（CSR 形式）
# ─────────────────────────────────────────────────────────
el('次数・隣接リスト構築')
endpoints = np.concatenate([i1, i2])
link_of_end = np.concatenate([np.arange(L), np.arange(L)])

deg = np.bincount(endpoints, minlength=NN)
order = np.argsort(endpoints, kind='stable')
adj_link = link_of_end[order]                      # ノード順に並んだリンク番号
indptr = np.zeros(NN + 1, dtype=np.int64)
np.cumsum(deg, out=indptr[1:])

d = np.bincount(deg)
el('  ノード次数の分布:')
for k in range(len(d)):
    if d[k]:
        print(f'          次数 {k:>2} : {d[k]:>10,}  ({d[k] / NN * 100:5.1f}%)')
el(f'  次数2（畳める通過点）: {d[2] if len(d) > 2 else 0:,} 件')


def other_end(link, node):
    """リンク link の、node ではない側のノードインデックス"""
    return i2[link] if i1[link] == node else i1[link]


def next_link_at(node, cur):
    """次数2ノード node において、cur ではないもう一方のリンク"""
    a, b = indptr[node], indptr[node + 1]
    for k in range(a, b):
        if adj_link[k] != cur:
            return adj_link[k]
    return -1


# ─────────────────────────────────────────────────────────
# チェーン抽出
#   次数 != 2 のノードを起点に、次数2ノードを通り抜けて次の交差点まで辿る
# ─────────────────────────────────────────────────────────
el('チェーン抽出')
used = np.zeros(L, dtype=bool)
chains = []          # [(start_node, end_node, [link, ...], [forward?, ...]), ...]

anchor_nodes = np.where(deg != 2)[0]
for v in anchor_nodes:
    for k in range(indptr[v], indptr[v + 1]):
        e = adj_link[k]
        if used[e]:
            continue
        used[e] = True
        seq = [e]
        fwd = [i1[e] == v]                 # リンクを node1→node2 の向きで辿るか
        cur_node, cur_link = other_end(e, v), e
        while deg[cur_node] == 2:
            nx = next_link_at(cur_node, cur_link)
            if nx < 0 or used[nx]:
                break
            used[nx] = True
            seq.append(nx)
            fwd.append(i1[nx] == cur_node)
            cur_node, cur_link = other_end(nx, cur_node), nx
        chains.append((v, cur_node, seq, fwd))

# 残り = 全ノードが次数2の閉ループ。任意の1点で切る
loops = 0
for e0 in np.where(~used)[0]:
    if used[e0]:
        continue
    v = int(i1[e0])
    used[e0] = True
    seq = [e0]
    fwd = [True]
    cur_node, cur_link = int(i2[e0]), e0
    while cur_node != v:
        nx = next_link_at(cur_node, cur_link)
        if nx < 0 or used[nx]:
            break
        used[nx] = True
        seq.append(nx)
        fwd.append(i1[nx] == cur_node)
        cur_node, cur_link = other_end(nx, cur_node), nx
    chains.append((v, cur_node, seq, fwd))
    loops += 1

el(f'  チェーン {len(chains):,} 本（うち閉ループ {loops:,}）')
assert used.all(), f'未使用リンクが {int((~used).sum()):,} 本残った'

# ─────────────────────────────────────────────────────────
# 縮約リンクの構築
# ─────────────────────────────────────────────────────────
el('縮約リンク構築')
dist_m = links['dist_m'].astype('float64').to_numpy()
has_time = 'time_001min' in links.columns
time_arr = links['time_001min'].astype('float64').to_numpy() if has_time else None
geoms = links.geometry.to_numpy()
link_ids = (links['link_id'].to_numpy() if 'link_id' in links.columns
            else np.arange(L, dtype=np.int64))

rows = []
member_rows = []
for cid, (sv, ev, seq, fwd) in enumerate(chains):
    coords = []
    for e, f in zip(seq, fwd):
        c = list(geoms[e].coords)
        if not f:
            c = c[::-1]
        coords.extend(c if not coords else c[1:])
    rec = {
        'cid': cid,
        'node1': int(uniq[sv]),
        'node2': int(uniq[ev]),
        'dist_m': float(dist_m[seq].sum()),
        'n_member': len(seq),
        'geometry': LineString(coords),
    }
    if has_time:
        rec['time_001min'] = float(time_arr[seq].sum())
    rows.append(rec)

    # 対応表: 縮約リンク始点(node1)からの累積距離・累積所要時間と、
    #         その元リンクを「辿った向き」での始終点ノードID
    cum_m, cum_t = 0.0, 0.0
    for s, (e, f) in enumerate(zip(seq, fwd)):
        nf, nt = (n1[e], n2[e]) if f else (n2[e], n1[e])
        member_rows.append((cid, s, int(link_ids[e]), int(nf), int(nt),
                            cum_m, cum_t, float(dist_m[e]),
                            float(time_arr[e]) if has_time else 0.0))
        cum_m += float(dist_m[e])
        cum_t += float(time_arr[e]) if has_time else 0.0

out = gpd.GeoDataFrame(rows, crs=links.crs)
el(f'  縮約リンク {len(out):,} 本')

# 対応表（縮約リンク → 元 link_id・縮約リンク始点からの累積距離）
import pandas as pd
members = pd.DataFrame(member_rows, columns=[
    'cid', 'seq', 'link_id', 'node_from', 'node_to',
    'offset_m', 'offset_t', 'len_m', 'len_t'])

# ノードは縮約後に残ったものだけ
kept = np.unique(np.concatenate([out['node1'].to_numpy(), out['node2'].to_numpy()]))
nodes_out = nodes[nodes['node_id'].astype('int64').isin(kept)].reset_index(drop=True)

# ─────────────────────────────────────────────────────────
# 出力・検算
# ─────────────────────────────────────────────────────────
el('出力')
p_links = OUT_DIR / f'KSJ_N13-24_{CASE_OUT}_道路リンク.parquet'
p_nodes = OUT_DIR / f'KSJ_N13-24_{CASE_OUT}_道路ノード.parquet'
p_memb = OUT_DIR / f'KSJ_N13-24_{CASE_OUT}_縮約対応表.parquet'
out.to_parquet(p_links)
nodes_out.to_parquet(p_nodes)
members.to_parquet(p_memb)

print()
print('===== 縮約結果 =====')
print(f'  リンク   : {L:,} → {len(out):,}  ({len(out) / L * 100:.1f}% ・約 {L / len(out):.1f} 分の1)')
print(f'  ノード   : {NN:,} → {len(nodes_out):,}  ({len(nodes_out) / NN * 100:.1f}%)')
print(f'  対応表   : {len(members):,} 行')
print()
print('  【検算】距離・所要時間は加算で保存されるはず')
print(f'    dist_m 合計       : {dist_m.sum():,.1f} → {out["dist_m"].sum():,.1f}'
      f'   差 {out["dist_m"].sum() - dist_m.sum():+.6f}')
if has_time:
    print(f'    time_001min 合計  : {time_arr.sum():,.1f} → {out["time_001min"].sum():,.1f}'
          f'   差 {out["time_001min"].sum() - time_arr.sum():+.6f}')
print(f'    対応表の元リンク数: {members["link_id"].nunique():,} （元の {L:,} と一致すべき）')
print()
for p in (p_links, p_nodes, p_memb):
    print(f'  {p.name}  {p.stat().st_size / 1e6:.1f} MB')
el('完了')
