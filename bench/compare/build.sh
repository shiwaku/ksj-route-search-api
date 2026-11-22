#!/usr/bin/env bash
# 経路比較ビューワの準備: 範囲の切り出し → OSM PBF → 3 エンジンの前処理 → 起動
#   bash bench/compare/build.sh            # 東京 23 区くらい（tokyo23）
set -euo pipefail
export MSYS_NO_PATHCONV=1      # Git Bash が /data/... を Windows のパスに書き換えないように
cd "$(dirname "$0")/../.."
CASE=${CASE:-tokyo23}

uv run python preprocess/clip_network.py --case "$CASE"
uv run --with osmium python preprocess/export_osm_pbf.py "$CASE"

cd bench/compare
rm -rf data && mkdir -p data
cp "../../network/$CASE/roads_$CASE.osm.pbf" data/roads.osm.pbf
dc() { docker compose run --rm --no-deps "$@"; }
dc osrm osrm-extract -p /conf/ksj.lua /data/roads.osm.pbf
dc osrm osrm-partition /data/roads.osrm
dc osrm osrm-customize /data/roads.osrm
dc valhalla valhalla_build_tiles -c /conf/valhalla.json /data/roads.osm.pbf
docker compose up -d          # GraphHopper は起動時に取り込む（graph-cache）
echo "起動しました。GraphHopper の取り込みが終わるまで数十秒かかります: docker compose -f bench/compare/docker-compose.yml logs -f graphhopper"
