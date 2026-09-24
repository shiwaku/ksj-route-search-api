#!/bin/zsh
# /bench 用の比較テーブル roads_jp を compose の DB に投入する（geom 付き・約 15 秒・1.0 GB）
# 入力: network/nationwide/pgrouting_nationwide.csv（export_for_pgrouting.py の出力。id,source,target,cost,reverse_cost,geom(hex WKB)）
# 使い方: docker compose up -d が済んだ状態で  zsh preprocess/load_pgrouting_table.sh
set -e
cd "$(dirname "$0")/.."
P() { docker compose exec -T -e PGPASSWORD=route db psql -h localhost -U route -d route -v ON_ERROR_STOP=1 "$@"; }
echo "start $(date +%T)"
P -c "DROP TABLE IF EXISTS roads_stage; CREATE UNLOGGED TABLE roads_stage(id bigint, source bigint, target bigint, cost double precision, reverse_cost double precision, geom text);"
P -c "\copy roads_stage FROM STDIN CSV HEADER" < network/nationwide/pgrouting_nationwide.csv
echo "staged $(date +%T)"
P -c "DROP TABLE IF EXISTS roads_jp_new; CREATE TABLE roads_jp_new AS SELECT id, source, target, cost, reverse_cost, ST_SetSRID(ST_GeomFromWKB(decode(geom,'hex')),4326)::geometry(LineString,4326) AS geom FROM roads_stage;"
echo "converted $(date +%T)"
P -c "ALTER TABLE roads_jp_new ADD PRIMARY KEY (id); CREATE INDEX ON roads_jp_new(source); CREATE INDEX ON roads_jp_new(target); CREATE INDEX ON roads_jp_new USING gist(geom); ANALYZE roads_jp_new;"
P -c "DROP TABLE roads_jp; ALTER TABLE roads_jp_new RENAME TO roads_jp; DROP TABLE roads_stage;"
P -c "SELECT count(*), pg_size_pretty(pg_total_relation_size('roads_jp')) FROM roads_jp;"
echo "done $(date +%T)"
