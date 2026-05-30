# Route Search API

国土数値情報（KSJ）道路データを使った経路探索・到達圏 Web API。

**ビューワー**: https://shiwaku.github.io/ksj-route-search-api/

## API

| エンドポイント | 説明 |
|---|---|
| `GET /healthz` | 死活確認 |
| `POST /reachability` | 始点から N 分以内の到達圏（道路リンク単位） |
| `POST /route` | 最短経路（始点→終点） |

### POST /reachability

```json
{ "lat": 35.8578, "lon": 139.6490, "max_min": 30, "mode": "vehicle" }
```

レスポンス: `{ "links": {"link_id": dist_min, ...}, "meta": {...} }`

### POST /route

```json
{ "orig_lat": 35.8578, "orig_lon": 139.6490, "dest_lat": 36.0420, "dest_lon": 139.4006, "mode": "vehicle" }
```

レスポンス: `{ "link_ids": [123, 456, ...], "meta": { "dist_min": 53.4 } }`

## ビューワーの使い方

GitHub Pages で公開したビューワーは、API サーバーの URL を `?api=` パラメータで指定します。

```
https://shiwaku.github.io/ksj-route-search-api/?api=https://your-api-server.com
```

PMTiles を別途 S3 等に置く場合:

```
?api=https://your-api&pmtiles=https://your-bucket.s3.amazonaws.com/roads.pmtiles
```

## ローカル起動

```bash
# 依存インストール
pip install -r requirements.txt

# PMTiles 生成（初回のみ）
python3 src/make_pmtiles.py

# サーバー起動
uvicorn src.main:app --host 0.0.0.0 --port 8080

# ビューワー
open http://localhost:8080/?api=http://localhost:8080
```

## ネットワーク

`network/saitama/` に道路リンク・ノード parquet を配置（gitignored）。

```bash
# saitama_all（全道路・フィルターなし）を 06_TimesliceReachability-link からコピー
cp ../06_TimesliceReachability-link/network/saitama/*.parquet network/saitama/
```

| ファイル | 内容 |
|---|---|
| `KSJ_N13-24_saitama_all_道路リンク.parquet` | 949,637 本（全道路） |
| `KSJ_N13-24_saitama_all_道路ノード.parquet` | 706,418 件 |

## パフォーマンス（saitama_all）

| 処理 | 時間 |
|---|---|
| 起動時グラフ構築 | 約 25 秒（1 回のみ） |
| `/reachability` vehicle 30 分 | 約 0.6 s |
| `/route` vehicle | 約 0.1 s |
