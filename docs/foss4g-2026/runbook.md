# FOSS4G 当日の公開手順（11/27 前日〜11/28 当日〜発表後）

FOSS4G Hokkaido 2026（11/28・トナリエ北広島）で、来場者にスマホから公開版を触ってもらうための手順。
公開版が使えないときの予備は [backup-demo.md](backup-demo.md)、設定の中身は [deploy/cloudflare/README.md](../../deploy/cloudflare/README.md)。

- 公開版: https://ksj-route-search.shi-works-worker.workers.dev
- 本番デプロイと Cloudflare ダッシュボードの操作は、本人が手で行う（自動モードでは止められる）

## いまの状態（2026-11-26）

| | 状態 |
|---|---|
| 本番のバージョン | 62545f37（11/23 デプロイ・main の 44eccc6 と同じ） |
| 台数 | standard-1 × 4（`CONTAINER_COUNT: "4"`・`max_instances: 4`） |
| 寝るまで | 10 分（`SLEEP_AFTER: "10m"`） |
| 限定公開 | Cloudflare Access で本人だけ |
| 本番に出ていない main の変更 | #58 のエラー文の表記（「道路網」→「道路ネットワーク」）だけ |

負荷試験（11/23・[記録](../bench/2026-11-23/README.md)）では、4 台で混在 100 人の最遅が 2.7 秒・エラー 0。起こすのに 1 台あたり約 70 秒。

## 決めておくこと

| | 案 | 理由 |
|---|---|---|
| 寝るまでの時間 | 当日だけ `2h` に上げる（11/27 にデプロイ） | 10 分だと、朝に起こしてから発表までに寝てしまい、発表の最初のタップで約 70 秒待つ。起きている間の費用は 4 台で 1 時間 約 $0.15 |
| デプロイの日 | 11/27（前日）の夜 | 当日の朝にイメージのビルドで詰まると取り返せない。上げないなら、デプロイ自体を省いてよい（#58 は文言だけ） |
| Access を外す時刻 | 11/28 の朝、確認が済んだあと | 外すと誰でも開けるので、前日から外す理由はない |

## 11/27（前日）

### 1. 寝るまでの時間を上げてデプロイする（上の案を採るとき）

デプロイ専用の作業ツリー `../ksj-route-search-api-deploy`（`network/` が実ディレクトリ）を使う。Docker Desktop を起動しておく。

```bash
cd ../ksj-route-search-api-deploy
git fetch && git checkout --detach origin/main
# deploy/cloudflare/wrangler.jsonc の vars.SLEEP_AFTER を "2h" に書き換える（台数 4 はそのまま）
cd deploy/cloudflare && npm run deploy
```

この書き換えは main にコミットしない（発表後に 10m へ戻すため）。コミットするなら発表後の戻しと組にする。

デプロイ後の確認（Access があるので、ログイン済みのブラウザで開く）:

- `/api/health?instance=0` 〜 `instance=3` を順に開き、全部 `"status":"ok"` と `walk.loaded: true`
- 応答ヘッダ `X-Container-Instance` が開いた番号と一致する

### 2. 出典表示を目で見る（#53）

スマホと PC で公開版を開き、画面の帰属表示に国土数値情報の出典が出ていることを確かめる。#53 はこれで閉じられる。

### 3. 回数制限（429）が効くか確かめる（#25）

ログイン済みのブラウザで公開版を開き、開発者ツールのコンソールで流す。同じ端末から 10 秒に 20 回を超えたら 429 になるはず（大まかにしか効かないので、30 回のうち数回 429 が出れば十分）。

```js
const id = 'runbook-test-0001';
const codes = [];
for (let i = 0; i < 30; i++) {
  const r = await fetch('/api/route?from_lat=43.0687&from_lon=141.3508&to_lat=43.0620&to_lon=141.3544&use_expressway=false', { headers: { 'X-Client-Id': id } });
  codes.push(r.status);
}
console.log(codes.join(' '));
```

429 が 1 本も出なくても、コンテナ側の同時計算の上限（2 本）があるので落ちはしない。そのときは記録だけ残す。

### 4. Access を外す画面を見ておく（まだ外さない）

Workers & Pages → `ksj-route-search` → **Access** タブを開き、保護を外す操作がどこにあるかを見ておく。
公式ドキュメントには「Access タブで該当する Access ルールを無効にする」とだけあり、ボタンの名前は書かれていない（未確認）。
Workers & Pages の一覧にある **Protect all Workers**（アカウント全体の保護）が有効になっていないことも確かめる。有効だと、Worker の保護を外してもこちらで守られたままになる。

### 5. 予備デモを用意する

[backup-demo.md](backup-demo.md) の ② を発表用 PC で一度起動し、札幌駅の到達圏と経路が出ることを確かめる。③ の録画が無ければ撮る。

## 11/28（当日）

### 朝（会場に着いたら）

1. 発表用 PC で予備デモ（②）を起動しておく
2. Access を外す（前日に見た Access タブで）
3. シークレットウィンドウとスマホ（モバイル回線）で公開版を開き、ログイン画面が出ないこと
4. 全台を起こす。Access を外したあとなら curl で届く:

   ```bash
   URL=https://ksj-route-search.shi-works-worker.workers.dev
   for i in 0 1 2 3; do curl -s -w " ← $i 台目 %{time_total}s\n" "$URL/api/health?instance=$i"; done
   ```

   寝ていた台は約 70 秒かかる。全台 `"status":"ok"` になるまで繰り返す
5. 会場の Wi-Fi につないだスマホで、会場（トナリエ北広島）をタップして到達圏が出ること、経路を 1 本引けること

### 発表の 10 分前

- `SLEEP_AFTER` を 10m のままにしたときは、ここで 4. をもう一度流して全台を起こす（2h にしていれば不要）
- 公開版を開いたタブと、予備デモのタブを両方用意しておく

### 発表中に公開版が駄目になったら

| 症状 | 対応 |
|---|---|
| 画面が開かない・API がずっと返らない | 予備デモ（②）のタブに切り替える |
| 429 が来場者に出る | 発表後に `wrangler.jsonc` の `ratelimits` の `limit` を上げて再デプロイ（README「運用」）。発表中は触らない |
| 1 台だけ遅い | `X-Container-Instance` で台を確かめる。発表中は触らない |

## 発表後（11/28 のうちに）

1. Access を付け直す（本人だけ）。手順は [deploy/cloudflare/README.md](../../deploy/cloudflare/README.md)「限定公開」の 1〜5 と、ポリシーの Include を **Emails** = 本人のアドレスにする。外したときに One-time PIN やログイン方法の設定が消えたかは未確認なので、シークレットウィンドウで開いてメール入力だけの画面になるかで確かめる
2. 1 台・10 分に戻してデプロイする: `wrangler.jsonc` を `CONTAINER_COUNT: "1"`・`max_instances: 1`・`SLEEP_AFTER: "10m"` にし、デプロイ専用の作業ツリーから `npm run deploy`。この変更は PR にして main に入れる
3. 1〜3 台目は使われなくなり、寝るまで（2h にしたなら最大 2 時間）で止まる。翌日、Cloudflare のダッシュボードで費用を確かめる
