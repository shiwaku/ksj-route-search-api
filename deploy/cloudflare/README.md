# Cloudflare Workers + Containers で一般公開する

設計は [`docs/deploy-cloudflare.md`](../../docs/deploy-cloudflare.md)。構成は [shiwaku/npa-traffic-accident-analytics `deploy/cloudflare/`](https://github.com/shiwaku/npa-traffic-accident-analytics/tree/main/deploy/cloudflare) を踏襲し、Static Assets・R2・Rate Limiting を足した。

```
ブラウザ → Worker（src/worker.ts）─┬─ /api/*   → Container（FastAPI・DB なし・グラフ焼き込み）
                                  ├─ /tiles/* → R2（roads_nationwide.pmtiles・Range → 206）
                                  └─ それ以外 → Static Assets（web/build）
```

| ファイル | 役割 |
|---|---|
| `wrangler.jsonc` | Assets / R2 / Rate Limiting / Container（`standard-1`・`max_instances: 1`）をまとめて定義 |
| `src/worker.ts` | パスで振り分け。`/api` は `/health` `/reachability` `/route` だけ通し、重い 2 本は IP ごとに 120 回/分 |
| `Dockerfile` | ルートの Dockerfile ＋ グラフ parquet（380 MB）。`DATABASE_URL` を設定しない = ブックマークと `/bench` は無い |
| `../../.dockerignore` | 許可リスト。wrangler は Dockerfile を標準入力で渡すので、Dockerfile ごとの ignore は効かない |
| `../../web/static/.assetsignore` | 道路タイル（200 MB）を Assets から外す。Assets は 1 ファイル 25 MiB まで |

## 実測（ローカル Docker・`--memory=4g --cpus=0.5` = standard-1 相当・2026-09-26）

| | |
|---|---|
| 起動（`/health` が `ok` になるまで） | 34 秒（うちグラフ読み込み 28.4 秒） |
| 常駐メモリ / 起動中のピーク | 2.35 GiB / 3.16 GiB |
| 到達圏 30 / 120 / 480 分 | 0.18 / 0.57 / 0.81 秒 |
| 経路 東京→横浜 / 東京→大阪 | 0.04 / 0.76 秒 |
| 120 分を 30 本同時 | 落ちない（最後の 1 本は約 20 秒待ち） |

実機（コールドスタート・R2 経由のタイル・スリープ）はデプロイ後に測る（設計書 8 章 V4〜V6）。

## デプロイ手順

Docker Desktop が動いていること（wrangler がイメージをビルドする）。`network/nationwide/` に parquet 2 本と PMTiles があること（`preprocess/` で生成・git 管理外）。

```bash
cd deploy/cloudflare
npm install
npx wrangler login

# ① 道路タイルを R2 に置く（初回と、PMTiles を作り直したときだけ）
npx wrangler r2 bucket create ksj-route-tiles
npx wrangler r2 object put ksj-route-tiles/roads_nationwide.pmtiles \
  --file ../../network/nationwide/roads_nationwide.pmtiles --remote

# ② 画面を build して Worker・コンテナをデプロイ（初回はイメージ 2.7 GB の push で数分〜）
npm run deploy

# ③ 確認。寝ている状態からの初回は起動待ちで遅い
curl https://ksj-route-search.shi-works-worker.workers.dev/api/health
```

**グラフと PMTiles は必ず揃えて更新する。** 到達圏は `link_id`（parquet の行番号）でタイルの道路を塗るので、片方だけ新しいと色がずれる（`docs/api-design.md` 5 章）。parquet を変えたら ② で、PMTiles を変えたら ① でアップロードし直す。

## 運用

- **スリープ**: 最後のリクエストから 10 分で寝る（`sleepAfter`）。寝ている間は課金されない。稼働中かどうかは `npx wrangler containers list` で ID を調べ、`npx wrangler containers instances <ID>` の `STATE` を見る（`LIVE INSTANCES` は寝ていても 1 のままで使えない。お手本の README）
- **ログ**: `npx wrangler tail`、または Cloudflare ダッシュボードの Workers → ksj-route-search → Logs（`observability` 有効）
- **回数制限に当たる**（画面に `429: リクエストが多すぎます`）なら `wrangler.jsonc` の `ratelimits[0].simple.limit` を上げて `npx wrangler deploy`
- **費用の上限**は standard-1 を 24 時間起動し続けて約 $32/月（`max_instances: 1`）。設計書 7 章

## 分かっている落とし穴

- **`wrangler dev` は Windows でコンテナを動かせない。** Worker の振り分けと R2 だけなら、`wrangler.jsonc` をコピーして `"dev": { "enable_containers": false }` を足したもので `npx wrangler dev -c <コピー>` できる（`/api/*` は 500 になる）。ローカル R2 へは `wrangler r2 object put ... --local`
- **ローカルの `wrangler dev` では Rate Limiting が効かない**（125 回連打しても 429 にならなかった）。実機で確かめる
- **ルートの `.dockerignore` は許可リスト。** API に新しいディレクトリやファイルを足したら、ここにも足さないとイメージに入らない
