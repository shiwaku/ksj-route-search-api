# デプロイ設計書（Cloudflare）

Route Search API を Cloudflare Workers + Containers で公開する

| | |
|---|---|
| 版 | 0.3（2026-09-26・デプロイ済み https://ksj-route-search.shi-works-worker.workers.dev ・V1〜V5 済み） |
| 対象 | 探索系（`/health` `/reachability` `/route`）と画面。**DB（PostgreSQL / PostGIS）は持っていかない** |
| お手本 | [shiwaku/npa-traffic-accident-analytics `deploy/cloudflare/`](https://github.com/shiwaku/npa-traffic-accident-analytics/tree/main/deploy/cloudflare)（2026-09-21 から同じ構成で運用中） |
| 置き換える決定 | [`api-design.md` ADR-10「デプロイ: しない」](api-design.md#adr-10-デプロイしないローカルデモ) |

**この文書は「どこに何を置くか」と「なぜそうしたか（9 章・ADR）」。** API の仕様そのものは `api-design.md` のまま変えない。

---

## 0. 構成図

```mermaid
flowchart LR
  B["ブラウザ<br/>SvelteKit + MapLibre"]

  subgraph CF["Cloudflare（1 つの Worker・1 つの URL）"]
    direction TB
    W["Worker<br/>src/worker.ts<br/>パスで振り分けるだけ"]
    A["Static Assets<br/>SvelteKit の build 出力<br/>styles/pale.json"]
    R2["R2<br/>roads_nationwide.pmtiles<br/>200 MB"]
    C["Container（standard-1）<br/>FastAPI そのまま<br/>グラフ parquet 380 MB を焼き込み"]
  end

  GSI["国土地理院<br/>最適化ベクトルタイル"]

  B -->|"/ ・ /_app/* ・ /styles/*"| W
  B -->|"/tiles/*（Range）"| W
  B -->|"/api/*"| W
  W --> A
  W -->|"R2 バインディング<br/>206 Partial Content"| R2
  W -->|"/api を剥がして転送<br/>getContainer('main')"| C
  B -. 背景地図は直接 .-> GSI
```

破線以外はすべて同じオリジン。**CORS が要らない**（ADR-D2）。

---

## 1. 前提と範囲

| # | 前提 | 理由 |
|---|---|---|
| ① | **DB を使わない。** `/bookmarks` と `/bench`（pgRouting 比較）は公開しない | 公開版の芯は「全国で 1 秒以内の到達圏・経路」。ブックマークは状態を持つため、Cloudflare に Postgres がない以上、外部 DB（Neon など）か D1 への移植が要る。その手間に見合わない |
| ② | **ローカルの 3 層構成は壊さない** | `docker compose up -d` で PostGIS・ブックマーク・pgRouting 比較が今までどおり動くこと。DB を外すのは環境変数で切り替える（5 章） |
| ③ | **API のコードは探索部分を変えない** | `graph.py` `core.py` はそのまま。変えるのは起動時の分岐と `/health` だけ |
| ④ | インスタンスは 1 台（`max_instances: 1`） | デモ用途。費用の上限をはっきりさせる（7 章） |

---

## 2. 構成要素

| 要素 | Cloudflare のサービス | 中身 | 置き場所（リポジトリ） |
|---|---|---|---|
| 受付 | **Worker** | パスで Assets / R2 / Container に振り分ける。数十行 | `deploy/cloudflare/src/worker.ts` |
| 画面 | **Workers Static Assets** | `web/` を `adapter-static` で build した出力 | `web/build/`（wrangler.jsonc の `assets.directory` から参照） |
| 道路タイル | **R2** | `roads_nationwide_N13-24.pmtiles`（200 MB・1 ファイル） | 共有バケット `shi-works` の `pmtiles/ksj-route-search/`（バケットのキー規約に従う。版ごとに別キー。手順は `deploy/cloudflare/README.md`） |
| API | **Containers** | FastAPI（今の `Dockerfile` とほぼ同じ）＋グラフ parquet | `deploy/cloudflare/Dockerfile` |
| 設定 | wrangler | 上 4 つをまとめて定義 | `deploy/cloudflare/wrangler.jsonc` |

お手本との違いは **Static Assets と R2 バインディングが増える**ことだけ。Worker → Container の部分は同じ書き方。

---

## 3. ルーティング

| パス | 行き先 | 備考 |
|---|---|---|
| `/api/health` `/api/reachability` `/api/route` | Container | `/api` を剥がして `/health` などとして渡す。`/docs` `/openapi.json` も同様に通す |
| `/api/bookmarks*` `/api/bench*` | — | Container 側でルーター自体を登録しないので 404（5 章） |
| `/tiles/roads_nationwide.pmtiles` | R2 | `Range` ヘッダをそのまま `R2Bucket.get(key, { range })` に渡し、`206` + `Content-Range` で返す。`HEAD` にも応える |
| 上記以外 | Static Assets | SPA なので未知のパスは `index.html`（`not_found_handling: "single-page-application"`） |

`/api/*` と `/tiles/*` だけ Worker のコードを通し、それ以外は Assets が直接返す（`run_worker_first: ["/api/*", "/tiles/*"]`）。

---

## 4. コンテナ

### 4-1. インスタンスサイズ: standard-1（1/2 vCPU・4 GiB・8 GB disk）

| | 実測（ローカル・2026-09-25） | standard-1 | standard-2 |
|---|---|---|---|
| メモリ | 常駐 **2.15 GiB**・起動中のピーク **3.16 GiB**（2026-09-26・PR #2 の `malloc_trim` 後。前は 3.14 / 3.59） | 4 GiB（余裕 0.8〜1.8 GiB） | 6 GiB |
| CPU | 16 コアの PC で到達圏 480 分が 1.1 秒 | 1/2 vCPU | 1 vCPU |
| 24 時間起動の費用 | — | 約 $0.92/日 | 約 $1.40/日 |

**standard-1 で始め、8 章の計測で足りなければ standard-2 に上げる。** 変更は `instance_type` の 1 行。
メモリの余裕が薄いので、落ちるとしたら起動時のピーク（parquet を丸ごと読む瞬間）。削減策は `api-design.md` 1-3 追記にある（row group ごとの逐次読み・int32 化で 300 MB 級）。

### 4-2. グラフのデータ: イメージに焼き込む

| 案 | コールドスタート | デプロイ | 判定 |
|---|---|---|---|
| **(a) イメージに `COPY`** | ダウンロードなし | parquet を変えたときだけ 380 MB のレイヤが上がる | **採用** |
| (b) 起動時に R2 から取得（お手本の方式） | 毎回 380 MB を取得してから読む | イメージが小さい | 不採用 |

お手本が (b) なのは、データ（事故統計）をイメージと別に更新したいから。こちらのグラフは国土数値情報の年次更新でしか変わらず、変えるときは PMTiles と `link_id` を揃えて作り直す必要がある（`api-design.md` 5 章の注意）。**グラフとコードが同じイメージに入っている方が、`link_id` がずれる事故が起きない。**

イメージは今の API イメージ 1.95 GB ＋ parquet 0.38 GB で約 2.3 GB。上限（= disk 8 GB）の内側。

**2026-09-26 変更（issue #7）: parquet ではなく、組み立て済みのグラフ（`.npy` 群・674 MiB）を焼く。**
parquet から組み立てると、実機で起動のたびに 43.9 秒掛かっていた（WKB の変換・ID の重複除去・文字列 ID の変換など、毎回同じ結果になる処理）。
`preprocess/build_graph_arrays.py` で一度だけ組み立てて書き出し、API は起動時に読むだけにした。
実機の読み込みは 43.9 秒 → 9.8 秒、ローカル（1/2 vCPU）は 28.4 秒 → 2.9 秒、常駐メモリは 2.35 GiB → 1.02 GiB。イメージは約 2.9 GB。
組み立て済みのグラフも parquet・PMTiles と同じ版名（`KSJ_N13-24_…`）で揃える。以下の「parquet」は組み立て済みのグラフと読み替える。
レイヤの順は「依存 → parquet → `api/`」にして、コードだけ直したときは小さいレイヤしか上がらないようにする。
ルートの `.dockerignore` を許可リストにして parquet を含める（実装時に変更）。当初は Dockerfile ごとの ignore（`Dockerfile.dockerignore`）を置く案だったが、wrangler は Dockerfile を標準入力（`-f -`）で渡すのでそれが効かない。compose 用の Dockerfile は parquet を `COPY` しないので、そちらのイメージは変わらない。

### 4-3. 起動とスリープ

| | 設定 | 根拠 |
|---|---|---|
| `sleepAfter` | **`10m`** | お手本と同じ。寝ている間は課金されない |
| 起動中の応答 | 今のまま。ポートはすぐ開き、グラフ構築中の `/health` は `status: loading`、探索系は 503 | `api-design.md` 2-4。画面は既に `/health` を見て待つ |
| コールドスタートの見込み | コンテナ起動（お手本の実機で初回 79 秒・provisioning 込み。2 回目以降は未計測）＋グラフ読み込み（ローカル 18 秒・1/2 vCPU で延びる） | **8 章で実測する**。1 分を超えるなら画面に「起動中（初回は約 N 秒）」を出す |

---

## 5. API 側の変更

切り替えは **`DATABASE_URL` が設定されているかどうか**だけで行う。compose は今まで同様に設定し、Cloudflare では設定しない。

| 箇所 | `DATABASE_URL` あり（ローカル） | なし（Cloudflare） |
|---|---|---|
| `db.py` の既定値 `postgresql://route:route@localhost:5433/route` | そのまま | **既定値をやめる**（未設定 = DB なし、と読めるようにする） |
| `main.py` lifespan の `init_db` | 実行 | 実行しない |
| `bookmarks_router` `bench_router` | 登録 | **登録しない**（404） |
| `/health` の `db` | 毎回接続して `ok` / `error: …` | **キーを返さない**。接続を試みない（試みると `connect_timeout=3` で `/health` が毎回 3 秒待つ） |
| CORS | `http://localhost:5173` を許可 | 同一オリジンなので不要（残っていても害はない） |

`/health` に `features: ["bookmarks", "bench"]` のような一覧を足し、画面はそれを見て UI を出し分ける（6 章）。

---

## 6. 画面側の変更

| 箇所 | 今 | 変更後 |
|---|---|---|
| `vite.config.ts` の adapter | `adapter-auto` | **`adapter-static`**（`fallback: 'index.html'`）。`+page.ts` は既に `ssr = false` |
| API のベース URL（`lib/api.ts`） | `PUBLIC_API_URL ?? 'http://localhost:8000'` | **`/api`** に統一。ローカルは Vite の `server.proxy` で `/api` → `localhost:8000`（パスを剥がす）。本番もローカルも同一オリジンになる |
| PMTiles の URL（`+page.svelte`） | `${location.origin}/roads_nationwide.pmtiles`（Vite の静的配信） | **`${location.origin}/tiles/roads_nationwide.pmtiles`**。ローカルは `static/tiles/` にリンクを移す |
| ブックマーク UI | 常に出す | `/health` の `features` に `bookmarks` があるときだけ出す |

---

## 7. 費用

料金は [Containers Pricing](https://developers.cloudflare.com/containers/pricing/)（2026-09-25 時点）。Workers Paid（$5/月）が前提。

| 使い方 | standard-1 の月額目安 |
|---|---:|
| ほぼ使わない（無料枠 25 GiB-時 ≒ standard-1 で約 6 時間/月の内側） | **$5** |
| 平日に 2 時間ずつ見せる（2h × 22 日 ＋ 各 10 分の `sleepAfter`） | 約 $6.5 |
| 24 時間起きっぱなし | 約 $32 |

- **上限は $32/月**（`max_instances: 1` なので、それ以上は増えない）。切り忘れても 1 日約 $0.92
- CPU は実使用だけの課金で、到達圏 1 回 1 秒程度なのでほぼゼロ
- R2 は 200 MB の保管が無料枠（10 GB）の内側。R2 からの送信は無料。Worker のリクエスト数も Paid の枠（1,000 万/月）の内側
- 送信量: 到達圏 480 分で gzip 1.7 MB。1 TB の無料枠に対して十分小さい

---

## 8. 検証計画

`wrangler dev` は Windows ではコンテナを動かせない（お手本の README）。**ローカルの Docker で制限をかけて測り、その後に実機で測る。**

| # | 何を | どうやって | 合格の目安 |
|---|---|---|---|
| V1 | standard-1 でメモリが足りるか | `docker run --memory=4g --cpus=0.5` で起動し、`limit_min=480` を連打 | OOM で落ちない → **合格**（下記） |
| V2 | 1/2 vCPU での起動時間 | V1 の `/health` の `load_seconds` | 60 秒以内 → **28.4 秒**（parquet 焼き込みの公開版イメージ） |
| V3 | 1/2 vCPU での探索時間 | 東京駅起点 30 / 120 / 480 分、東京→大阪の `/route` | 480 分で 3 秒以内 → **合格**（下記） |
| V4 | 実機のコールドスタート | お手本の `probe.py` の要領で、寝ている状態から `/api/health` が `ok` になるまで | 記録するだけ → **49 秒**（応答開始 6.4 秒・グラフ読み込み 43.9 秒。ローカルより遅い） |
| V5 | R2 経由のタイル | ブラウザの Network で `/tiles/*` が 206、パンが重くない | 206・目視で許容 → curl で 206・中身一致を確認。ブラウザの目視は未 |
| V6 | スリープ | 10〜16 分後に `wrangler containers instances <ID>` の `STATE` が `inactive` | `inactive`（`LIVE INSTANCES` は寝ていても 1 のままなので使わない） |

### V1〜V3 の結果（ローカル Docker・2026-09-26）

最初の計測で **120 分 × 8 本同時に OOM** した（常駐 3.14 GiB に結果の配列が重なった）。一般公開で数十人が同時に使う想定なので、PR #2 で直してから測り直した。

| | PR #2 前 | PR #2 後 |
|---|---:|---:|
| 到達圏 30 / 120 / 480 分 | 0.73 / 3.87 / 2.16 秒 | 0.18 / 0.57 / 0.81 秒 |
| 経路 東京→横浜 / 東京→大阪 | 0.97 / 1.02 秒 | 0.04 / 0.76 秒 |
| 常駐 / 起動中のピーク | 3.14 / 3.59 GiB | 2.15 / 3.16 GiB |
| 120 分を同時に投げたとき | 8 本で OOM | 30 本でも落ちない（ピーク 2.73 GiB・最後の 1 本は約 20 秒待ち） |

直したこと: 到達圏の配列を orjson で直接書き出す（120 分の 3.5 秒は探索ではなく JSON 化だった）、到達圏の同時計算を 2 本に制限、起動後の `malloc_trim`、経路探索に上限（打ち切りなしで毎回全国を探していた）。
**standard-1 で足りる。** 待ちを減らしたくなったら standard-2（1 vCPU）。

---

## 9. 技術選定の記録（ADR）

### ADR-D1 実行基盤: Cloudflare Containers（AWS ではなく）

| 選択肢 | 判定 | 理由 |
|---|---|---|
| **Cloudflare Containers** | **採用** | 既存の Docker イメージがほぼそのまま動く。寝ている間は無料で、上限も読める。**別リポジトリで同じ構成を運用中**なので、手順も落とし穴も分かっている |
| Workers（コンテナなし） | 不可 | メモリ上限 128 MB。グラフは 3 GB |
| AWS（EC2 / Fargate） | 不採用 | ADR-10 の見積もりどおり t3.large 級で構築に半日〜1 日。EC2 は寝かせる仕組みを自前で作らない限り常時起動の固定費になる |

### ADR-D2 画面の置き場所: Worker の Static Assets（Pages ではなく）

| 選択肢 | 判定 | 理由 |
|---|---|---|
| **Static Assets（API と同じ Worker）** | **採用** | 画面・タイル・API が 1 つのオリジンに揃い、**CORS も API の URL の環境変数も要らない**。デプロイも `wrangler deploy` 1 回 |
| Pages ＋ 別 Worker | 不採用 | `*.pages.dev` と `*.workers.dev` でオリジンが分かれ、CORS の許可リストとビルド時の API URL を管理することになる |

### ADR-D3 PMTiles: R2 を Worker 経由で配る

| 選択肢 | 判定 | 理由 |
|---|---|---|
| **R2 バインディング（Worker が Range を処理）** | **採用** | 同一オリジン（ADR-D2）。カスタムドメインが要らない |
| R2 の公開バケット（カスタムドメイン） | 次点 | Range は R2 がそのまま処理し、CDN キャッシュも効く。ただしバケットの CORS 設定とドメインが要る。V5 でタイルが重ければこちらに切り替える |
| Static Assets に入れる | 不可 | 1 ファイル 25 MiB の上限。PMTiles は 200 MB |

### ADR-D4 DB: 持っていかない

1 章 ①。ブックマークを公開版でも使いたくなったら、「Hyperdrive ＋ 外部 Postgres（PostGIS あり）」と「D1 へ移植（点と属性だけなので geometry 型は不要）」を比べて決める。
`api-design.md` 1-3 の「計算はメモリ、状態は DB」という分け方は、状態を持たない公開版ではそのまま「計算だけ」になるだけで、崩れない。

---

## 10. 未確定

| # | 項目 | 状況 |
|---|---|---|
| ① | ~~誰に公開するか~~ | **一般公開に決定**（2026-09-26）。認証は付けない |
| ② | ~~重いリクエストの制限~~ | **決定**: コンテナ内で到達圏の同時計算を 2 本に制限（PR #2）＋ Worker の Rate Limiting で `/api/reachability` `/api/route` を IP ごとに 10 秒 20 回。会場や社内の Wi-Fi では同じ IP を大勢で共有するので緩めにした。**Rate Limiting は大まかにしか効かない**（実機で 60 秒 120 回は 300 回連打しても効かず、並列だと上限を超えて通る）ので歯止め程度。落ちない保証は同時計算の制限で持つ |
| ③ | ドメイン | 当面は `*.shi-works-worker.workers.dev`。独自ドメインにするかは後で |
| ④ | 起動中の画面表示 | V4 が 49 秒で 1 分以内。画面は「API 起動中…（しばらく使われていないと 1 分ほど掛かります）」と出す（前は「約 5 秒」でローカル向けだった） |
| ⑤ | ~~standard-1 で足りるか~~ | **足りる**（8 章 V1〜V3 の結果） |
