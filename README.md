# Route Search API

国土数値情報（KSJ）道路データを使った経路探索・到達圏 Web ビューワー・API。

道路データ：国土数値情報 道路データ / 測量法に基づく国土地理院長承認（使用）R 8JHs 85

---

## ビューワーの使い方

```
https://shiwaku.github.io/ksj-route-search-api/?api=https://shiworks2.xsrv.jp/api
```

FastAPI サーバーの URL を `?api=` パラメータで指定する。デプロイ手順は [SERVER_DEPLOY.md](SERVER_DEPLOY.md) を参照。

### 操作方法

| 操作 | 内容 |
|---|---|
| 左クリック | 始点を設定（緑マーカー） |
| 右クリック | 終点を設定（赤マーカー・経路探索タブ） |
| 「到達圏を表示」 | 始点から N 分以内の道路を色分け表示 |
| 「経路を表示」 | 始点→終点の最短経路を表示 |
| 「クリア」 | 表示結果をリセット |

---

## API エンドポイント（API サーバーモード時）

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

`mode`: `"vehicle"`（車）または `"walk"`（徒歩 3.6 km/h）

### POST /route

```json
{ "orig_lat": 35.8578, "orig_lon": 139.6490,
  "dest_lat": 36.0420, "dest_lon": 139.4006, "mode": "vehicle" }
```

レスポンス: `{ "link_ids": [123, 456, ...], "meta": { "dist_min": 53.4 } }`

---

## ローカル起動（FastAPI サーバーモード）

```bash
# 依存インストール
pip install -r requirements.txt

# ネットワークデータを配置（gitignored）
# network/saitama/ に道路リンク・ノード parquet を置く

# PMTiles 生成（初回のみ・tippecanoe 必要）
python3 src/make_pmtiles.py

# バイナリネットワーク生成（初回のみ・サーバーレス用）
python3 src/make_network_bin.py

# サーバー起動
uvicorn src.main:app --host 0.0.0.0 --port 8080

# ビューワー（API モード）
# ブラウザで http://localhost:8080/?api=http://localhost:8080 を開く
```

---

## ファイル構成

```
docs/
  index.html        ビューワー（GitHub Pages）
  pale.json         背景地図スタイル（国土地理院）
  roads.pmtiles     道路ネットワーク PMTiles（94MB・表示用）

src/
  main.py           FastAPI アプリ
  graph.py          RouterGraph（scipy Dijkstra）
  make_pmtiles.py   道路リンク parquet → PMTiles 変換
  benchmark.py      ライブラリ別速度比較

network/saitama/    gitignored（要配置）
  KSJ_N13-24_saitama_all_道路リンク.parquet  (61MB)
  KSJ_N13-24_saitama_all_道路ノード.parquet  (18MB)
```

---

## ネットワークデータの生成

```bash
# 国土数値情報（N13-24）GeoJSON から生成:
python3 src/ksj_to_network_csv.py \
  --meshes 5338,5339,5438,5439 --case saitama_all --pref 埼玉県
# ※ GeoJSON は https://nlftp.mlit.go.jp/ksj/ からダウンロード
```

| ファイル | 内容 |
|---|---|
| `KSJ_N13-24_saitama_all_道路リンク.parquet` | 949,637 本（全道路・フィルターなし） |
| `KSJ_N13-24_saitama_all_道路ノード.parquet` | 706,418 件 |

---

## パフォーマンス（saitama_all・埼玉県）

| 処理 | 時間 |
|---|---|
| 起動時グラフ構築 | 約 25 秒（1 回のみ） |
| `/reachability` vehicle 30 分 | 約 0.6 s |
| `/route` vehicle | 約 0.1 s |

---

## Xserver へのデプロイ

詳細は [SERVER_DEPLOY.md](SERVER_DEPLOY.md) を参照。

---

## 制約

- **一方通行未考慮**: 国土数値情報に一方通行フィールドなし（全道路双方向）
- **対象エリア**: デフォルトは埼玉県（saitama_all）
