# Route Search API — 全国版・3層アプリケーション

国土数値情報（KSJ）道路データを使った**全国規模の到達圏分析・経路探索**。
SvelteKit / FastAPI / PostgreSQL の3層構成。

埼玉県版・2層構成の [shiwaku/ksj-route-search-api-legacy](https://github.com/shiwaku/ksj-route-search-api-legacy) を土台に、
全国規模へ拡張し3層アーキテクチャへ作り替えたもの。

道路データ：国土数値情報 道路データ / 測量法に基づく国土地理院長承認（使用）R 8JHs 85

---

## 分析上の前提

| # | 前提 | 理由 |
|---|---|---|
| ① | 道路は**幅員5.5m以上**に絞る（`N13_003 in [1,2,4]` or `N13_006 in [3,4,5]`） | N13 は細街路を含み、全国では 2,400万フィーチャになる。DRM の「基本道路」に相当する範囲へ |
| ② | **移動モードは車のみ**（徒歩は対象外） | ①のフィルターと整合する。徒歩なら細街路こそが経路になる |
| ③ | 到達圏は**メッシュを使わず道路リンク単位** | 任意地点を始点にでき、道路形状そのままの粒度で描ける |
| ④ | 交差点間リンクへの縮約は**実装済み・未適用** | 実測の結果、素の全国グラフで十分な速度が出たため |
| ⑤ | **一方通行は考慮しない** | 国土数値情報の道路データに一方通行の属性が存在しない |
| ⑥ | 所要時間は道路分類ごとの**固定速度**で計算: 高速 80 / 国道 35 / 都道府県道 30 / 市区町村道 20 km/h（`N13_003` 1〜4・その他 20） | N13 に速度・渋滞の属性はない。`preprocess/build_network_parquet.py` の `SPEED_KMH` |

いずれも精度を下げる割り切りであり、欠陥ではない。始点は最近傍の道路ノードにスナップされる。

---

## 実測値（全国・2026-09-08）

### ネットワーク

| 項目 | 値 |
|---|---|
| 入力 | 国土数値情報 道路中心線 GeoParquet・**160メッシュ**・2.4 GB |
| 生フィーチャ | 24,065,831 |
| フィルター後リンク | **3,881,601**（16.1%） |
| ノード | 3,711,035 |
| 総延長 | 374,867 km |
| 生成時間 | **48 秒** |

### 到達圏・経路探索

| 項目 | **scipy（in-memory）** | **pgRouting（PostgreSQL）** |
|---|---|---|
| 到達圏 30分（東京駅） | **11.1 ms** | 3,333 ms |
| 経路探索 東京→大阪 | **266.7 ms** | 3,324 ms |
| 結果 | 123,945 ノード / コスト 37,955 | **同一** |
| 起動 / 初回応答 | **5.1 秒**（ジオメトリ列を読まずにグラフ 3.7 秒 ＋ 経路用座標 1.2 秒） | 実質ゼロ |
| メモリ | 2.9 GB RSS（グラフ 4 本 ＋ 座標 1,808 万点） | 585 MB（PostgreSQL） |
| データ更新 | 再起動が必要 | `UPDATE` で即反映 |

**結果は完全に一致し、速度は 10〜27 倍の差がつく**（pgRouting のエッジ SQL を bbox で絞った実務的な使い方。30 分 17 ms vs 315 ms、上限 480 分 216 ms vs 3.4 秒。絞らずに全 388 万行を毎回読む素朴な使い方だと 30 分で 260 倍。`/bench` で 30〜480 分を通して確認）。
差はアルゴリズムではなく「グラフをどこに置くか」から来る——
pgRouting は打ち切っても先にテーブル全体を読む必要があるため、探索範囲を変えても時間がほぼ一定になる。

→ **採用**: 探索グラフは in-memory、PostgreSQL はアプリデータ（保存地点）を持つ。
pgRouting は比較のために計測し、選択の根拠として示す。
**技術選定の全記録は [`docs/api-design.md` 8 章（ADR）](docs/api-design.md#8-技術選定の記録adr)。**

---

## 構成

```
.
├── docs/         API 設計書（api-design.md）・スクリーンショット（img/）
├── preprocess/   ネットワーク生成・縮約・ベンチマーク・タイル化
├── api/          FastAPI（graph.py 探索グラフ / main.py / bookmarks.py / db.py / core.py）
├── web/          SvelteKit + Tailwind + MapLibre（static/ に PMTiles へのシンボリックリンク）
├── docker-compose.yml   PostgreSQL / PostGIS（ブックマーク用・localhost:5433）
├── input/        国土数値情報 原データ（gitignored・約 5.6 GB）
└── network/      生成したネットワーク（gitignored・約 1.1 GB）
```

### api

| ファイル | 役割 |
|---|---|
| `graph.py` | 全国グラフを起動時にメモリへ（scipy CSR × 4 本: 時間/距離 × 高速あり/なし、座標 1,808 万点）。スナップは連結成分 1,000 ノード以上に限定。到達圏は `road_class` で絞り上限 100 万本 |
| `main.py` | FastAPI 本体。`/health` `/reachability` `/route`。gzip lv1・CORS・lifespan |
| `bookmarks.py` | `/bookmarks` CRUD ＋ `/{id}/reachability`（PostGIS・psycopg3・生 SQL） |
| `db.py` | 接続とスキーマ（起動時に `CREATE TABLE IF NOT EXISTS`） |
| `core.py` | 共有状態と到達圏レスポンスの組み立て |

### preprocess

| スクリプト | 役割 |
|---|---|
| `build_network_parquet.py` | GeoParquet → 道路リンク/ノード parquet。**既存実装と出力一致を検証済み・約11倍速** |
| `contract_network.py` | 次数2ノードを畳んで交差点間リンク化（3.8倍減・距離と時間は完全保存） |
| `router.py` | 縮約グラフ + リンクスナップ + 両端2始点 Dijkstra。**縮約前と誤差 0** |
| `bench_dijkstra.py` | 縮約前後の速度比較 |
| `export_for_pgrouting.py` | pgRouting 用 CSV 書き出し + scipy 側の正解算出 |
| `ksj_to_network_csv.py` | 既存実装（`ksj-reachability-analysis` からのコピー・比較用） |
| `build_graph_arrays.py` | リンク/ノード parquet → 組み立て済みの探索グラフ（`.npy` 群・674 MiB）。API の起動が parquet からの組み立て（1/2 vCPU 29 秒）から読むだけ（2.9 秒）になる。公開版のイメージにはこれを焼く |
| `make_pmtiles.py` | リンク parquet → PMTiles。**全国 146 MB**（z4-13・ズーム別に道路種別を出し分け: z5-8 高速・国道 / z9-10 ＋県道 / z11- 全部。低ズームは簡略化。タイル 1 枚は展開後 1.5 MB・圧縮後 0.4 MB 以下。`--wsl` で Windows から WSL の tippecanoe を使う。`--no-zoom-filter` `--max-tile-bytes` あり） |
| `bench_scale.py` | 規模別ベンチ（scipy / NetworkX）。**入れ子6規模・同一始点** |
| `bench_pgrouting_scale.py` | 規模別ベンチ（pgRouting）。ジオメトリなしで投入 |

---

## 使い方

```bash
uv sync

# 全国ネットワークを生成（48秒）
uv run python preprocess/build_network_parquet.py --nationwide --case nationwide --filter

# 単一メッシュで既存実装との一致を検証
uv run python preprocess/build_network_parquet.py --mesh 6441 --case 6441_pq --filter \
    --verify-against 6441_drm

# 縮約（任意）
uv run python preprocess/contract_network.py --case 6441_drm
uv run python preprocess/router.py --case 6441_drm --limit 3000 --validate 10

# 道路タイルを生成（全国 193 MB・1.5分）
uv run python preprocess/make_pmtiles.py --case nationwide          # Windows では --wsl（WSL に tippecanoe が要る）

# 探索グラフを組み立てて書き出す（全国 15 秒）。API はこれがあれば起動時に読むだけになる
uv run python preprocess/build_graph_arrays.py --case nationwide
```

parquet を作り直したら `build_graph_arrays.py` も実行し直す。API は `meta.json` に記録した parquet の大きさと
手元の parquet が違えば、組み立て済みを使わずに parquet から組み立てる（`/health` の `graph_source` が `parquet` になる）。

`input/geoparquet/N13-24_*.parquet` に国土数値情報の道路データを配置しておくこと。
画面は道路タイルを `web/static/tiles/roads_nationwide.pmtiles` から読むので、生成した PMTiles をそこにリンクかコピーで置く。

---

## ローカルで動かす（3 層）

すべて **リポジトリのルートで** 実行する。別のディレクトリだと ① が
「no configuration file provided: not found」（`docker-compose.yml` が見つからない）で止まる。

```bash
cd ksj-route-search-api
open -a Docker                                # ⓪ Docker Desktop が落ちていると①が "Cannot connect to the Docker daemon" で止まる
                                              #    起動には 20〜30 秒かかる。途中で①を叩くと "500 Internal Server Error ... /_ping" になるので、アイコンが止まるまで待つ
docker compose up -d                          # ① バックエンド一式: PostGIS（localhost:5433）+ API（localhost:8000）
                                              #    初回は API イメージの build に約 1 分。API はグラフ読込（7 秒）後に healthy になる。Swagger: /docs
cd web && npm install && npm run dev          # ② 画面 → http://localhost:5173
```

API の状態は `docker compose ps`（`healthy` になれば `/health` が `graph_loaded: true`）、ログは `docker compose logs -f api`。
API はコンテナで 3.0 GB 使う。Docker Desktop のメモリ上限（Settings → Resources）が 4 GB 未満だと起動に失敗する。

API のコードを直したら `docker compose up -d --build api`。コンテナを使わずホストで動かすとき（デバッガを付けたい・起動を速くしたい）は
`API_PORT=8001 docker compose up -d`（コンテナ側を 8001 に退避）してから `DATABASE_URL=postgresql://route:route@localhost:5433/route uv run uvicorn api.main:app --port 8000`。
`DATABASE_URL` を付けないと DB なし（公開版と同じ・ブックマークと `/bench` が 404）で起動する。
探索グラフの parquet（`network/`・git 管理外）はイメージに焼かず、compose の bind mount で `/app/network` に読み取り専用で渡している。

終了は ② のタブで `Ctrl+C`、バックエンドは次で片付ける（ボリュームは残るのでブックマークは消えない）:

```bash
docker compose down        # ① を片付ける（api と db）
docker desktop stop        # Docker Desktop も止めるなら。動いているコンテナがある状態で止めない（下の RWLayer エラーの原因）
```

① が「RWLayer of container … is unexpectedly nil」で止まるときは、Docker Desktop の終了時にコンテナが
中途半端に止められて書き込みレイヤが壊れている。コンテナだけ捨てて作り直す（データはボリュームにあるので消えない）:

```bash
docker rm -f ksj-route-db && docker compose up -d
```

DB を直接見るときはコンテナ内の psql を使う（ホストに psql は入っていない）。
`-h localhost` を省くと peer 認証で弾かれる。

```bash
docker compose exec -T -e PGPASSWORD=route db psql -h localhost -U route -d route -c '\dt'
```

地図をクリックすると到達圏が色づき（既定 120 分・高速あり/なし切替）、「経路探索」で 2 点クリックすると最短経路、
ブックマークで地点を保存・再表示できる。DB が無くても①以外は動く（ブックマークだけ 503）。

| 到達圏 120 分（関東・20 分刻み） | 480 分（本州広域・自動で高速＋国道のみ・80 分刻み） | 経路 |
|---|---|---|
| ![](docs/img/reach_120_wide_z8.png) | ![](docs/img/reach_480_major_z6.png) | ![](docs/img/route.png) |

背景は国土地理院 最適化ベクトルタイル（淡色地図風・`web/static/styles/pale.json`）。

画面の一気通貫テスト: `node web/e2e.mjs <出力dir>`（Playwright・headless Chromium）。

`GET /bench`（scipy vs pgRouting の比較用）を使うときだけ、比較用テーブルを DB に投入する（約 15 秒・1.0 GB）:

```bash
zsh preprocess/load_pgrouting_table.sh
curl 'localhost:8000/bench?limit_min=30'             # 全 388 万行を毎回読む素朴な使い方 → scipy 13 ms / pgRouting 3.3 秒
curl 'localhost:8000/bench?limit_min=30&bbox=true'   # エッジを bbox で絞る実務的な使い方 → scipy 17 ms / pgRouting 0.3 秒
```

---

## 一般公開（Cloudflare）

Cloudflare Workers + Containers で公開する（DB なし。ブックマークと `/bench` は出ない）。設計は [`docs/deploy-cloudflare.md`](docs/deploy-cloudflare.md)、手順は [`deploy/cloudflare/README.md`](deploy/cloudflare/README.md)。
画面の API は本番もローカルも同一オリジンの `/api`（ローカルは Vite の proxy が `/api` を剥がして `localhost:8000` に渡す）。

## API

**設計書: [`docs/api-design.md`](docs/api-design.md)**
（エンドポイント仕様・レスポンス形式・テーブル定義・性能の実測値）
`/docs`（Swagger UI）と `/openapi.json` が自動生成される。

### 配信するデータ

到達圏 API は**ジオメトリを返さない**。道路の形は静的なので PMTiles として一度だけ配り、
API が返す `link_id → コスト` を MapLibre の `setFeatureState` で重ねて着色する。
全国・30分の到達圏は 123,945 リンクあり、GeoJSON で返すと約 25 MB になるため。

| | 全国 | メッシュ6441 |
|---|---|---|
| PMTiles（z6-13） | **200 MB** | 6 MB |
| 生成時間 | **1.5 分** | 数十秒 |
| ズーム別の出し分け | z6-10 高速・国道・都道府県道 / z11- 全部（`-j` フィルター・タイル上限 1.5 MB） | 同左 |
| タイルの属性 | `road_type` のみ（`link_id` は**フィーチャ ID**） | 同左 |

z13 での間引きが起きていないことは、東京駅周辺のタイル `13/7276/3225` を
デコードして確認済み（タイル 3,976 本 ≧ 同 bbox の元リンク 3,686 本。差は境界バッファ）。

---

## 実装上の注意

### `link_id` は parquet の行番号という暗黙の約束

リンク parquet に `link_id` 列は**無い**。`router.py`・`export_for_pgrouting.py`・
`make_pmtiles.py` の 3 つが「行インデックス ＝ link_id」で揃えている。
**ここがずれるとタイルの着色が全部狂う。**

なお tippecanoe は `link_id = 0` にフィーチャ ID を付けられず
（`Can't represent too-large feature ID 0`）、`--use-attribute-for-id` は属性を
ID へ**移動**するため、その 1 本はタイル上で link_id を失う（388万本中 1 本）。
ID をずらすと他の 2 つと 1 ずれるので、ずらさず記録に留めている。

### `csr_matrix` は重複エッジを合計する

```python
csr_matrix((data, (rows, cols)))   # 同じ (row, col) が複数あると値が加算される
```

同じノード対を結ぶ並行リンク（中央分離帯で上下線が別、ループ橋など）で重みが過大になり、
経路が狂う。**重複は最小値に畳んでから渡す**こと（`preprocess/router.py` の `_csr()`）。

メッシュ6441 の実測で縮約前 39 対・縮約後 140 対が該当し、
直すまで最大 3145（＝31.45分）の誤差が出ていた。
`shiwaku/ksj-route-search-api` の `src/graph.py` と
`shiwaku/ksj-reachability-analysis` の `src/reachability_search.py` にも同じパターンがある。
