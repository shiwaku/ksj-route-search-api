#!/usr/bin/env python3
"""
道路ネットワーク parquet → PMTiles 変換（一回だけ実行）

出力: network/{case}/roads.pmtiles
  - レイヤー名  : roads
  - フィーチャID: link_id（行インデックス・API の link_id と一致）
  - ズーム      : Z6〜Z14

前提: tippecanoe がインストール済みであること
  sudo apt install tippecanoe   # Ubuntu 24.04+
  brew install tippecanoe       # macOS

使い方:
  python3 src/make_pmtiles.py
  python3 src/make_pmtiles.py --links network/saitama/KSJ_N13-24_saitama_all_道路リンク.parquet \
                               --out   network/saitama/roads.pmtiles
"""

import argparse
import subprocess
import tempfile
from pathlib import Path

import geopandas as gpd

REPO_ROOT    = Path(__file__).parent.parent
DEFAULT_LINKS = str(REPO_ROOT / "network/saitama/KSJ_N13-24_saitama_all_道路リンク.parquet")
DEFAULT_OUT   = str(REPO_ROOT / "docs/roads.pmtiles")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--links", default=DEFAULT_LINKS, help="道路リンク parquet")
    ap.add_argument("--out",   default=DEFAULT_OUT,   help="出力 PMTiles パス")
    args = ap.parse_args()

    out_path = Path(args.out)

    print(f"読み込み: {args.links}")
    links = gpd.read_parquet(args.links)
    links = links.reset_index(drop=True)

    if links.crs and links.crs.to_epsg() != 4326:
        links = links.to_crs(4326)

    # link_id = 行インデックス（API の dist_min dict のキーと一致）
    links["link_id"] = links.index.astype(int)

    with tempfile.NamedTemporaryFile(suffix=".geojson", delete=False, mode="w") as f:
        tmp = Path(f.name)

    print(f"GeoJSON 書き出し中... ({len(links):,} リンク)")
    links[["link_id", "geometry"]].to_file(str(tmp), driver="GeoJSON")

    print("tippecanoe 実行中...")
    cmd = [
        "tippecanoe",
        "-Z6", "-z14",
        "-l", "roads",
        "--use-attribute-for-id", "link_id",   # link_id をフィーチャ ID に昇格
        "--no-feature-limit",
        "--no-tile-size-limit",
        "--force",
        "-P",
        "-o", str(out_path),
        str(tmp),
    ]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)
    tmp.unlink()

    sz = out_path.stat().st_size // (1024 * 1024)
    print(f"完了: {out_path}  ({sz} MB)")


if __name__ == "__main__":
    main()
