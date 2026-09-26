# Cloudflare Workers + Containers で一般公開する

公開 URL: **https://ksj-route-search.shi-works-worker.workers.dev**（2026-09-26 デプロイ）

設計は [`docs/deploy-cloudflare.md`](../../docs/deploy-cloudflare.md)。構成は [shiwaku/npa-traffic-accident-analytics `deploy/cloudflare/`](https://github.com/shiwaku/npa-traffic-accident-analytics/tree/main/deploy/cloudflare) を踏襲し、Static Assets・R2・Rate Limiting を足した。

```
ブラウザ → Worker（src/worker.ts）─┬─ /api/*   → Container（FastAPI・DB なし・グラフ焼き込み）
                                  ├─ /tiles/* → R2 バケット shi-works の PMTiles（Range → 206）
                                  └─ それ以外 → Static Assets（web/build）
```

| ファイル | 役割 |
|---|---|
| `wrangler.jsonc` | Assets / R2 / Rate Limiting / Container（`standard-1`・`max_instances: 1`）をまとめて定義 |
| `src/worker.ts` | パスで振り分け。タイルのキーは `TILE_KEY`。読んだ範囲は Cache API に置く（下の「タイルのキャッシュ」）。`/api` は `/health` `/reachability` `/route` だけ通し、重い 2 本は IP ごとに 10 秒 20 回（大まかにしか効かない・下の落とし穴） |
| `Dockerfile` | ルートの Dockerfile ＋ 組み立て済みのグラフ（674 MiB・`preprocess/build_graph_arrays.py`）。parquet は入れない。`DATABASE_URL` を設定しない = ブックマークと `/bench` は無い |
| `../../.dockerignore` | 許可リスト。wrangler は Dockerfile を標準入力で渡すので、Dockerfile ごとの ignore は効かない |
| `../../web/static/.assetsignore` | 道路タイル（200 MB）を Assets から外す。Assets は 1 ファイル 25 MiB まで |

## 実測

| | ローカル Docker（`--memory=4g --cpus=0.5`） | **Cloudflare 実機**（standard-1・2026-09-26） |
|---|---:|---:|
| 起動（寝ている状態から `/api/health` が `ok` まで） | 34 秒（読み込み 28.4 秒）→ **6.4 秒**（読み込み 2.9 秒・組み立て済みのグラフ・#7） | 49 秒（応答開始 6.4 秒・読み込み 43.9 秒）→ 読み込み **9.8 秒**（#7）。寝ている状態からの合計は未計測 |
| 常駐メモリ / 120 分を 30 本同時のピーク | 2.35 / 2.73 GiB → **1.02 / 1.34 GiB**（#7） | — |
| 到達圏 30 / 120 / 480 分 | 0.18 / 0.57 / 0.81 秒 | 0.98 / 1.47 / 1.78 秒（手元 → Cloudflare の往復込み） |
| 経路 東京→横浜 / 東京→大阪 | 0.04 / 0.76 秒 | 0.29 / 1.24 秒（同上） |
| 120 分を 30 本同時 | 落ちない（最後の 1 本は約 20 秒待ち） | 未計測 |
| タイル（`/tiles/…`・Range 16 KB） | — | 206・中身が元ファイルと一致。R2 から 0.18〜0.29 秒、キャッシュから約 0.09 秒 |
| 画面の初回表示（ブラウザのキャッシュなし・playwright） | — | Worker のキャッシュが空 4.7 秒 → 当たる 2.1 秒 |

スリープ（10 分で `inactive` になるか・設計書 8 章 V6）は未確認。

## R2 のデータ

道路タイル（PMTiles）は、共有バケット **`shi-works`** に置く。このバケットは `shi-works.com` で公開していて、キーの付け方にルールがある
（`xserver-cleanup` リポジトリの `R2-STRUCTURE.md`）。

| | |
|---|---|
| キー | `pmtiles/ksj-route-search/roads_nationwide_<版>.pmtiles`（今は `roads_nationwide_N13-24_v2.pmtiles`） |
| 第 1 階層 `pmtiles/` | アクセス方法を表す（R2-STRUCTURE.md §4）。ルート直下に置かない |
| 第 2 階層 `ksj-route-search/` | プロジェクト名 |
| ファイル名の `<版>` | グラフ parquet（`KSJ_N13-24_…`）と揃える。同じデータでタイルの作り方だけ変えたら `_v2`, `_v3` … を付ける（`_v2` は z5〜8 を高速・国道だけにした版・issue #17）。**版ごとに別キー**にする（下の「データの更新」） |
| メタデータ | `content-type: application/octet-stream`、`cache-control: public, max-age=3600`（バケットの規約） |
| 読む側 | Worker が R2 バインディング `TILES` で読み、`/tiles/roads_nationwide.pmtiles` として返す（同一オリジン）。キーは `src/worker.ts` の `TILE_KEY` |
| 公開 URL | `https://shi-works.com/pmtiles/ksj-route-search/roads_nationwide_N13-24_v2.pmtiles` でも読める（バケットの CORS 設定済み） |

アップロード（どちらでもよい。リポジトリ直下で実行）:

```bash
# wrangler（ログインは npx wrangler login）
npx wrangler r2 object put shi-works/pmtiles/ksj-route-search/roads_nationwide_N13-24_v2.pmtiles \
  --file network/nationwide/roads_nationwide_v2.pmtiles \
  --content-type application/octet-stream --cache-control "public, max-age=3600" --remote

# aws CLI（バケットの規約に載っている手順。プロファイル r2-shiworks）
aws s3 cp network/nationwide/roads_nationwide_v2.pmtiles \
  s3://shi-works/pmtiles/ksj-route-search/roads_nationwide_N13-24_v2.pmtiles --profile r2-shiworks \
  --content-type application/octet-stream --cache-control "public, max-age=3600" --only-show-errors
```

確認: `curl -I https://shi-works.com/pmtiles/ksj-route-search/roads_nationwide_N13-24_v2.pmtiles` が 200 で、`Content-Length` が元ファイルと同じ。
**存在しないキーは Cloudflare の HTML の 404 になる**ので、ドメインの不具合と取り違えないこと（R2-STRUCTURE.md §6.6）。

## デプロイ手順（初回・コードだけ直したとき）

Docker Desktop が動いていること（wrangler がイメージをビルドする）。`network/nationwide/KSJ_N13-24_nationwide_graph/`（組み立て済みのグラフ）があること（`preprocess/build_graph_arrays.py` で生成・git 管理外）。

```bash
cd deploy/cloudflare
npm install
npx wrangler login
npm run deploy      # 画面を build → Worker・Assets・コンテナをデプロイ。初回はイメージ 2.7 GB の push で数分〜

curl https://ksj-route-search.shi-works-worker.workers.dev/api/health   # 寝ている状態からの初回は約 50 秒
```

コードだけを直したときは parquet のレイヤが変わらないので push は小さい。イメージが変わらなければ「Image already exists remotely, skipping push」になる。

## データの更新（国土数値情報の新しい版など）

**グラフ（イメージに焼いた parquet）と道路タイル（R2 の PMTiles）は必ず同時に切り替える。**
到達圏は `link_id`（parquet の行番号）でタイルの道路を塗るので、片方だけ新しいと色が別の道路に付く（`docs/api-design.md` 5 章）。
同じキーに上書きすると、イメージのデプロイとタイルの上書きの間に必ずずれる時間ができるので、**新しい版は別のキーに置く**。

1. `preprocess/` で parquet と PMTiles を作り直し、`build_graph_arrays.py` で組み立て済みのグラフも作り直す（`network/nationwide/`）。版名（`N13-24`）が変わるなら、`api/graph.py` の `KSJ_N13-24_…`、`Dockerfile` の `COPY` も合わせる。ローカル（compose + `npm run dev`）で到達圏の色が道路に乗ることを確かめる
2. 新しい PMTiles を**新しい版のキー**にアップロードする（上の「R2 のデータ」。例: `roads_nationwide_N13-25.pmtiles`）。この時点では誰も読んでいない
3. `src/worker.ts` の `TILE_KEY` を新しいキーに書き換える
4. `npm run deploy`。新しいグラフを焼いたイメージと、新しい `TILE_KEY` の Worker が一緒に出る
5. 画面で到達圏を出して、色が道路に乗っていることを確かめる。コンテナは入れ替わりで起動し直すので、最初の 1 回は約 50 秒待つ
6. 問題なければ、古い版のキーを消す（`npx wrangler r2 object delete shi-works/pmtiles/ksj-route-search/roads_nationwide_<古い版>.pmtiles --remote`）。
   戻したくなったら、古いキーを消す前なら手順 3〜4 を古いキーでやり直せばよい

キーの付け替えは、バケットの規約と同じ「新しいキーに置く → 疎通確認 → 参照元を更新 → デプロイ確認 → 旧キーを削除」の順（R2-STRUCTURE.md §6.7）。
ブラウザはタイルを最大 1 時間キャッシュする（`max-age=3600`）が、キーが変わると ETag も変わるので、PMTiles のクライアントは読み直す。
Worker のキャッシュ（下）もキーに `TILE_KEY` を含むので、キーを変えれば古い版は読まれない（1 日で消える）。

## タイルのキャッシュ

R2 から毎回読むと Range 1 回に約 0.2 秒掛かり、地図 1 画面で十数〜数十回読むので描画が遅かった（issue #4）。
Worker が読んだ範囲を Cache API（`caches.default`）に 1 日置き、2 回目以降はそこから返す。**Cache API は無料**で、R2 の読み取り回数も減る。

- `cache.put` は 206 を受け付けないので、「`TILE_KEY`＋範囲」をキーに 200 で保存し、返すときに 206 と `Content-Range` を付け直す
- キャッシュは**データセンター単位**（東京なら東京の利用者で共有）。そのデータセンターで最初にその範囲を読んだ人だけ R2 まで取りに行く
- 当たったかは応答ヘッダ `X-Tile-Cache: HIT / MISS` で分かる（`curl -s -D - -o /dev/null -H 'Range: bytes=0-16383' <URL>/tiles/roads_nationwide.pmtiles`）
- 4 MB を超える範囲、`Range` の無いリクエスト、`If-None-Match` 付きのリクエストはキャッシュを通さず R2 から返す
- `workers.dev` のドメインでも効く（2026-09-26 実測）

## 運用

- **スリープ**: 最後のリクエストから 10 分で寝る（`sleepAfter`）。寝ている間は課金されない。稼働中かどうかは `npx wrangler containers list` で ID を調べ、`npx wrangler containers instances <ID>` の `STATE` を見る（`LIVE INSTANCES` は寝ていても 1 のままで使えない。お手本の README）
- **ログ**: `npx wrangler tail`、または Cloudflare ダッシュボードの Workers → ksj-route-search → Logs（`observability` 有効）
- **回数制限に当たる**（画面に `429: リクエストが多すぎます`）なら `wrangler.jsonc` の `ratelimits[0].simple.limit` を上げて `npx wrangler deploy`（`period` は 10 か 60 のみ）
- **費用の上限**は standard-1 を 24 時間起動し続けて約 $32/月（`max_instances: 1`）。設計書 7 章

## 分かっている落とし穴

- **`wrangler dev` は Windows でコンテナを動かせない。** Worker の振り分けと R2 だけなら、`wrangler.jsonc` をコピーして `"dev": { "enable_containers": false }` を足したもので `npx wrangler dev -c <コピー>` できる（`/api/*` は 500 になる）。ローカル R2 へは `wrangler r2 object put ... --local`
- **Rate Limiting は大まかにしか効かない。** ローカルの `wrangler dev` では一切効かない（125 回連打しても 429 にならない）。実機でも `period: 60`・`limit: 120` は 300 回連打して一度も 429 にならず、`period: 10`・`limit: 10` は逐次なら 13 回目で 429、並列で投げると上限を超えても通ることがあった（2026-09-26）。公式にも「厳密な計数ではない」とある。落ちない保証はコンテナ側の同時計算 2 本（`api/core.py`）で持つ
- **ルートの `.dockerignore` は許可リスト。** API に新しいディレクトリやファイルを足したら、ここにも足さないとイメージに入らない
- **`/api/docs`（Swagger）は本番では開けない。** Worker が `/api` を剥がして転送し、`/docs` を通していないため
