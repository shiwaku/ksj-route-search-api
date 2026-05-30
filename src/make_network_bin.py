#!/usr/bin/env python3
"""
道路ネットワーク parquet → クライアントサイド Dijkstra 用バイナリ変換

出力: docs/net.bin（列指向・リトルエンディアン）
  [uint32] n_links          リンク数
  [uint32] n_nodes          ノード数
  [uint32 × n_links] node1_idx   始点ローカルインデックス
  [uint32 × n_links] node2_idx   終点ローカルインデックス
  [float32 × n_links] cost_v     vehicle コスト（分）
  [float32 × n_links] cost_w     walk コスト（分）
  [uint32 × n_links] link_id     PMTiles の link_id と一致（行インデックス）
  [float32 × n_nodes] node_lat   ノード緯度
  [float32 × n_nodes] node_lon   ノード経度

使い方:
  python3 src/make_network_bin.py
  python3 src/make_network_bin.py --links network/saitama/KSJ_N13-24_saitama_all_道路リンク.parquet
"""

import argparse
import struct
from pathlib import Path

import numpy as np
import geopandas as gpd

REPO_ROOT    = Path(__file__).parent.parent
DEFAULT_LINKS = str(REPO_ROOT / "network/saitama/KSJ_N13-24_saitama_all_道路リンク.parquet")
DEFAULT_NODES = str(REPO_ROOT / "network/saitama/KSJ_N13-24_saitama_all_道路ノード.parquet")
DEFAULT_OUT   = str(REPO_ROOT / "docs/net.bin")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--links", default=DEFAULT_LINKS)
    ap.add_argument("--nodes", default=DEFAULT_NODES)
    ap.add_argument("--out",   default=DEFAULT_OUT)
    args = ap.parse_args()

    print(f"読み込み: {args.links}")
    links = gpd.read_parquet(args.links)
    nodes = gpd.read_parquet(args.nodes)
    links = links.reset_index(drop=True)

    n1_raw = links["node1"].astype(int).to_numpy()
    n2_raw = links["node2"].astype(int).to_numpy()
    w_veh  = (links["time_001min"].astype(float) * 0.01).to_numpy(dtype=np.float32)
    w_walk = (links["dist_m"].astype(float) / 60.0).to_numpy(dtype=np.float32)
    link_ids = links.index.to_numpy(dtype=np.uint32)

    # ノード ID → ローカルインデックス
    unique = np.unique(np.concatenate([n1_raw, n2_raw]))
    n2i = {int(n): i for i, n in enumerate(unique.tolist())}
    n_nodes = len(unique)
    n_links = len(links)
    print(f"  リンク: {n_links:,}  ノード: {n_nodes:,}")

    idx1 = np.array([n2i[int(n)] for n in n1_raw], dtype=np.uint32)
    idx2 = np.array([n2i[int(n)] for n in n2_raw], dtype=np.uint32)

    # ノード座標（ローカルインデックス順）
    node_id_to_coord = {}
    for row in nodes.itertuples():
        node_id_to_coord[int(row.node_id)] = (float(row.geometry.y), float(row.geometry.x))

    lat_arr = np.zeros(n_nodes, dtype=np.float32)
    lon_arr = np.zeros(n_nodes, dtype=np.float32)
    for nid, idx in n2i.items():
        if nid in node_id_to_coord:
            lat_arr[idx], lon_arr[idx] = node_id_to_coord[nid]

    out = Path(args.out)
    print(f"書き出し: {out}")
    with open(out, "wb") as f:
        f.write(struct.pack("<II", n_links, n_nodes))
        f.write(idx1.tobytes())
        f.write(idx2.tobytes())
        f.write(w_veh.tobytes())
        f.write(w_walk.tobytes())
        f.write(link_ids.tobytes())
        f.write(lat_arr.tobytes())
        f.write(lon_arr.tobytes())

    sz = out.stat().st_size / (1024 * 1024)
    print(f"完了: {sz:.1f} MB")


if __name__ == "__main__":
    main()
