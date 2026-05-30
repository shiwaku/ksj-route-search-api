# Route Search API — Claude Code 向けプロジェクト仕様

## 概要

国土数値情報（KSJ）道路データを使った経路探索・到達圏 Web API。  
scipy Dijkstra でリクエストごとに探索し、結果は `link_id` と到達時間のみ返す（ジオメトリなし）。  
クライアント側で PMTiles（事前生成済み）に `setFeatureState` で色付けする設計。

## ディレクトリ構成

```
11_RouteSearchAPI/
├── src/
│   ├── main.py              # FastAPI アプリ（起動時グラフロード・エンドポイント定義）
│   ├── graph.py             # RouterGraph クラス（グラフ構築・Dijkstra・到達圏・経路）
│   ├── make_pmtiles.py      # 道路リンク parquet → PMTiles 変換（初回のみ実行）
│   ├── benchmark.py         # scipy / igraph / rustworkx 速度比較スクリプト
│   └── __init__.py
├── viewer/
│   ├── index.html           # MapLibre GL JS ビューワー（GitHub Pages 公開用）
│   └── pale.json            # 背景地図スタイル（国土地理院ベクトルタイル淡色）
├── docs/                    # GitHub Pages ソース（viewer/ と同期・Actions が自動更新）
│   ├── index.html
│   └── pale.json
├── .github/workflows/
│   └── deploy-pages.yml     # viewer/ → docs/ 同期 → GitHub Pages デプロイ
├── network/                 # gitignored（大容量データ）
│   └── saitama/
│       ├── KSJ_N13-24_saitama_all_道路リンク.parquet   (61MB)
│       ├── KSJ_N13-24_saitama_all_道路ノード.parquet   (18MB)
│       └── roads.pmtiles                                (68MB・make_pmtiles.py で生成)
├── requirements.txt
├── .gitignore
├── README.md
└── CLAUDE.md
```

## 起動コマンド

```bash
# 依存インストール
pip install -r requirements.txt

# PMTiles 生成（初回のみ・tippecanoe 必要）
python3 src/make_pmtiles.py

# サーバー起動（ポート 8080）
uvicorn src.main:app --host 0.0.0.0 --port 8080 --reload

# ビューワー（ローカル）
# http://localhost:8080/?api=http://localhost:8080

# ベンチマーク
python3 src/benchmark.py
```

## API 仕様

### GET /healthz
```json
{ "status": "ok", "graph_loaded": true }
```

### POST /reachability
```json
// リクエスト
{ "lat": 35.8578, "lon": 139.6490, "max_min": 30, "mode": "vehicle" }
// mode: "vehicle"（道路種別・幅員による速度テーブル）| "walk"（3.6 km/h 一律）
// max_min: 1〜240 分

// レスポンス
{
  "links": { "10617": 30.0, "52314": 5.2, ... },  // link_id → dist_min（分）
  "meta": { "snap_m": 17, "reachable_links": 270417 }
}
```

### POST /route
```json
// リクエスト
{ "orig_lat": 35.8578, "orig_lon": 139.6490,
  "dest_lat": 36.0420, "dest_lon": 139.4006, "mode": "vehicle" }

// レスポンス
{
  "link_ids": [52314, 52313, 52322, ...],  // 始点→終点順のリンク ID
  "meta": { "dist_min": 53.41, "link_count": 346, "orig_snap_m": 17, "dest_snap_m": 23 }
}
```

## graph.py の主要クラス・メソッド

### RouterGraph

| メソッド | 説明 |
|---|---|
| `__init__(links_path, nodes_path)` | parquet 読み込み・CSR 行列構築・KDTree・エッジ逆引きテーブル |
| `reachability(lat, lon, max_min, mode)` | scipy `dijkstra(limit=max_min)` → link_id: dist_min dict |
| `route(orig_lat, orig_lon, dest_lat, dest_lon, mode)` | scipy `dijkstra(return_predecessors=True)` → link_id リスト |

### コスト計算

| モード | コスト列 | 計算式 |
|---|---|---|
| vehicle | `time_001min` | `time_001min × 0.01`（分） |
| walk | `dist_m` | `dist_m / 60.0`（3.6 km/h = 60 m/分） |

### link_id

道路リンク parquet の行インデックス（0 始まり）。PMTiles の `--use-attribute-for-id link_id` と一致。

## ビューワー（viewer/index.html）

### URL パラメータ

| パラメータ | デフォルト | 説明 |
|---|---|---|
| `?api=` | `''`（未設定バナー表示） | FastAPI サーバー URL |
| `?pmtiles=` | `{api}/static/network/saitama/roads.pmtiles` | PMTiles URL |

### ローカル開発 URL
```
http://localhost:8080/?api=http://localhost:8080
```

### GitHub Pages 公開 URL
```
https://shiwaku.github.io/ksj-route-search-api/?api=https://your-api.example.com
```

### 描画の仕組み

1. `roads.pmtiles` を MapLibre source として常駐ロード（全道路グレー表示）
2. API レスポンスの `link_id → dist_min` を `setFeatureState({ dist_rank })` でセット
3. Paint expression の `match ['feature-state', 'dist_rank']` で色付け
4. `removeFeatureState` でリセット（新しいリクエスト前）

## パフォーマンス（saitama_all・949,637 本）

| 処理 | 時間 |
|---|---|
| 起動時 parquet 読み込み | 約 7.6 s |
| 起動時グラフ構築 | 約 17 s（合計） |
| `/reachability` vehicle 30 分 | **約 0.6 s**（Dijkstra 0.07s + JSON 0.5s） |
| `/reachability` walk 30 分 | **約 0.6 s**（到達リンク少なく JSON も速い） |
| `/route` | **約 0.1 s** |

## ネットワークデータ配置

`network/saitama/` は gitignored。`06_TimesliceReachability-link/network/saitama/` からコピー。

```bash
cp ../06_TimesliceReachability-link/network/saitama/KSJ_N13-24_saitama_all_*.parquet \
   network/saitama/
```

kanto_all など別エリアに切り替える場合は環境変数で指定:

```bash
LINKS_PATH=/path/to/kanto_all_道路リンク.parquet \
NODES_PATH=/path/to/kanto_all_道路ノード.parquet \
uvicorn src.main:app --port 8080
```

## PMTiles 生成（make_pmtiles.py）

```bash
python3 src/make_pmtiles.py \
  --links network/saitama/KSJ_N13-24_saitama_all_道路リンク.parquet \
  --out   network/saitama/roads.pmtiles
```

- tippecanoe が必要: `sudo apt install tippecanoe`
- `--use-attribute-for-id link_id`: link_id をフィーチャ ID に昇格
- saitama_all: 約 68 MB、生成時間 約 5〜10 分

## GitHub Pages

- ソース: `docs/` フォルダ（main ブランチ）
- `viewer/` を編集 → push → GitHub Actions が `docs/` に同期・デプロイ
- 設定: Settings → Pages → Source: Deploy from branch `main` / `docs`

## 制約

- **一方通行未考慮**: 国土数値情報に一方通行フィールドなし（全道路双方向）
- **ネットワーク範囲**: デフォルトは埼玉県（saitama_all・全道路・フィルターなし）
- **kanto_all 未生成**: 12 メッシュ・全道路版は別途 `ksj_to_network_csv.py` で生成が必要
