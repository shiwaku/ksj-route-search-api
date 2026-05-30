# Xserver へのFastAPI デプロイ手順

レンタルサーバー（Xserver 共有ホスティング）に FastAPI サーバーを構築し、PHP プロキシ経由で HTTPS として公開する手順。

## 前提条件

- Xserver 共有ホスティング（sv*.xserver.jp）
- SSH アクセス可能
- ドメイン設定済み（例：`shiworks2.xsrv.jp`）
- ローカル PC：WSL / macOS / Linux 環境

## サーバー環境

| 項目 | 値 |
|---|---|
| ホスト名 | `sv16193.xserver.jp` |
| SSH ポート | **10022**（標準の 22 ではない） |
| OS | Rocky Linux 8 |
| Apache | 2.4.x（共有）|
| ドキュメントルート | `~/[ドメイン名]/public_html/` |

---

## ステップ1：SSH 接続確認

Xserver の SSH ポートは **10022**。

```bash
ssh -p 10022 [ユーザー名]@[ホスト名]
# 例：ssh -p 10022 shiworks2@sv16193.xserver.jp
```

初回接続時はホスト鍵の確認が出るので `yes` と入力する。

---

## ステップ2：Python 環境構築（Miniconda）

Xserver のシステム Python は 3.6.8 と古く、FastAPI に対応していない。また pyenv でのコンパイルも開発ヘッダ不足で失敗するため、**Miniconda** を使用する。

```bash
# Miniconda インストーラーをダウンロード
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O ~/miniconda.sh

# インストール（非対話型）
bash ~/miniconda.sh -b -p ~/miniconda3

# bash に設定を追記
~/miniconda3/bin/conda init bash
source ~/.bashrc

# 確認（Python 3.13 が表示される）
python3 --version
pip3 --version
```

---

## ステップ3：Python パッケージのインストール

```bash
pip install fastapi uvicorn geopandas pyarrow scipy
```

---

## ステップ4：ローカル PC の SSH 鍵設定

Xserver は公開鍵認証のみ対応。ファイル転送（scp）のために WSL 側に鍵を作成する。

**ローカル PC（WSL）で：**

```bash
# SSH 鍵ペアを生成
ssh-keygen -t ed25519 -f ~/.ssh/id_xserver -C "xserver"
# パスフレーズは空でよければ Enter ×2

# 公開鍵を表示（この内容をコピーしておく）
cat ~/.ssh/id_xserver.pub
```

**サーバー側（SSH ターミナル）で：**

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh

# 公開鍵を登録（上でコピーした内容を貼り付ける）
echo '[公開鍵の内容]' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

**ローカル PC で接続テスト：**

```bash
ssh -p 10022 -i ~/.ssh/id_xserver [ユーザー名]@[ホスト名] echo "接続OK"
```

---

## ステップ5：リポジトリのクローンとネットワークデータ配置

**サーバー側で：**

```bash
cd ~
git clone https://github.com/shiwaku/ksj-route-search-api.git
mkdir -p ~/ksj-route-search-api/network/saitama
```

**ローカル PC（WSL）から parquet ファイルを転送：**

```bash
# 道路リンク（約 61MB）
scp -P 10022 -i ~/.ssh/id_xserver \
  "/path/to/KSJ_N13-24_saitama_all_道路リンク.parquet" \
  [ユーザー名]@[ホスト名]:~/ksj-route-search-api/network/saitama/

# 道路ノード（約 18MB）
scp -P 10022 -i ~/.ssh/id_xserver \
  "/path/to/KSJ_N13-24_saitama_all_道路ノード.parquet" \
  [ユーザー名]@[ホスト名]:~/ksj-route-search-api/network/saitama/
```

転送元のパスは環境に合わせて変更する。

---

## ステップ6：ポート疎通確認

Xserver ではファイアウォールにより高番号ポートへの**外部アクセスはブロックされている**。ただしサーバー内部からのアクセス（localhost）は可能。

```bash
# サーバー側でポートが使えるか確認（内部通信のみ）
python3 -m http.server 18080 &
sleep 2 && curl -s http://localhost:18080 | head -2
kill %1
```

> **注意**：`http://[サーバーIP]:18080` への外部直接アクセスは不可。Apache 経由（後述の PHP プロキシ）を使う。

---

## ステップ7：uvicorn 起動

```bash
cd ~/ksj-route-search-api

# バックグラウンドで起動（ログは ~/api.log に出力）
nohup uvicorn src.main:app --host 0.0.0.0 --port 18080 > ~/api.log 2>&1 &

# 起動確認（グラフ読み込みに 20〜30 秒かかる）
sleep 30 && tail -10 ~/api.log
```

正常起動時のログ：
```
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:18080 (Press CTRL+C to quit)
```

内部から動作確認：
```bash
curl -s http://localhost:18080/healthz
# → {"status":"ok","graph_loaded":true}
```

ポート 18080 がすでに使用中の場合：
```bash
fuser -k 18080/tcp
```

---

## ステップ8：PHP プロキシの設置

Xserver の共有 Apache では `.htaccess` での `ProxyPass` が使えないため、**PHP スクリプトをプロキシ**として使用する。

```bash
# API 用ディレクトリを作成
mkdir -p ~/[ドメイン名]/public_html/api
```

**proxy.php を作成：**

```bash
cat > ~/[ドメイン名]/public_html/api/proxy.php << 'EOF'
<?php
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: GET, POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type');
if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') { http_response_code(204); exit; }

$path = isset($_GET['_path']) ? $_GET['_path'] : '/';
$ch = curl_init('http://127.0.0.1:18080' . $path);
curl_setopt($ch, CURLOPT_CUSTOMREQUEST, $_SERVER['REQUEST_METHOD']);
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
if (in_array($_SERVER['REQUEST_METHOD'], ['POST','PUT'])) {
    $body = file_get_contents('php://input');
    curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
    curl_setopt($ch, CURLOPT_HTTPHEADER, ['Content-Type: application/json']);
}
$res = curl_exec($ch);
$code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$ct   = curl_getinfo($ch, CURLINFO_CONTENT_TYPE);
curl_close($ch);
http_response_code($code);
if ($ct) header('Content-Type: ' . $ct);
echo $res;
EOF
```

**.htaccess を作成：**

```bash
cat > ~/[ドメイン名]/public_html/api/.htaccess << 'EOF'
RewriteEngine On
RewriteRule ^healthz$       proxy.php?_path=/healthz      [QSA,L]
RewriteRule ^reachability$  proxy.php?_path=/reachability [QSA,L]
RewriteRule ^route$         proxy.php?_path=/route        [QSA,L]
EOF
```

**動作確認（ブラウザ）：**

```
https://[ドメイン名]/api/healthz
```

`{"status":"ok","graph_loaded":true}` が返れば成功。

---

## ステップ9：ビューワーと接続

GitHub Pages のビューワーに `?api=` パラメータで API URL を指定する。

```
https://shiwaku.github.io/ksj-route-search-api/?api=https://[ドメイン名]/api
```

---

## プロセス管理

### 起動確認

```bash
ps aux | grep uvicorn | grep -v grep
```

### ログ確認

```bash
tail -f ~/api.log
```

### 停止

```bash
pkill -f "uvicorn src.main:app"
```

### 再起動（サーバーが再起動した場合など）

```bash
cd ~/ksj-route-search-api
nohup uvicorn src.main:app --host 0.0.0.0 --port 18080 > ~/api.log 2>&1 &
```

> **注意**：Xserver 共有ホスティングではサーバー再起動時にプロセスが消える。永続化が必要な場合は cron で起動スクリプトを定期実行するか、VPS に移行することを検討する。

### cron で自動再起動（任意）

```bash
crontab -e
```

以下を追加：

```cron
*/5 * * * * pgrep -f "uvicorn src.main:app" > /dev/null || cd ~/ksj-route-search-api && nohup /home/[ユーザー名]/miniconda3/bin/uvicorn src.main:app --host 0.0.0.0 --port 18080 >> ~/api.log 2>&1 &
```

---

## アーキテクチャ図

```
[クライアント（ブラウザ）]
        │ HTTPS
        ▼
[Apache（Xserver 共有）]
  ~/[ドメイン]/public_html/api/
    .htaccess  → RewriteRule でルーティング
    proxy.php  → curl で localhost:18080 に転送
        │ HTTP（内部通信）
        ▼
[uvicorn（ポート 18080）]
  ~/ksj-route-search-api/
    src/main.py   → FastAPI
    src/graph.py  → RouterGraph（scipy Dijkstra）
    network/saitama/
      道路リンク.parquet（起動時にメモリロード）
      道路ノード.parquet
```

---

## トラブルシューティング

### `graph_loaded: false` が返る

起動直後はグラフ読み込み中。20〜30 秒待ってから再度確認する。

### uvicorn がすぐ終了する

ポートが使用中の可能性：
```bash
fuser -k 18080/tcp && sleep 2
```
その後 `nohup uvicorn ...` を再実行。

### scp で `Permission denied (publickey)` エラー

SSH 鍵が未設定。ステップ4を参照して `~/.ssh/authorized_keys` に公開鍵を登録する。

### API に接続できない（Mixed Content エラー）

GitHub Pages（HTTPS）から HTTP の API は呼べない。ステップ8の PHP プロキシ経由で HTTPS にすること。

### pyenv でのPythonコンパイルが失敗する

Xserver 共有ホスティングは `libbz2-devel`・`openssl-devel`・`libffi-devel` 等の開発ヘッダが利用できないためコンパイルが失敗する。**Miniconda を使うこと**（ステップ2参照）。
