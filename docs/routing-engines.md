# 経路探索エンジンの比較検証（scipy CSR / pgRouting / OSRM / Valhalla / GraphHopper）

2026-09-11（8 章の「同じ経路になるか」は 2026-10-05）。設計書 `docs/api-design.md` ADR-1 から分離した検証記録。
**目的**: 採用した scipy in-memory を、DB で探索する pgRouting だけでなく、経路探索の専用エンジン 3 つとも同じデータで並べ、選定の位置づけを数字で言えるようにする。

## 1. 候補 5 つの概要と特徴

| | scipy CSR（採用） | pgRouting | OSRM | Valhalla | GraphHopper |
|---|---|---|---|---|---|
| 何者か | Python 科学計算ライブラリの疎行列 + `csgraph.dijkstra`（C 実装）。経路探索専用ではない汎用部品 | PostgreSQL 拡張（C++・Boost Graph Library）。SQL 関数として探索する | Open Source Routing Machine。C++。Mapbox が育てた経路エンジン | C++。Mapzen 発・現在はコミュニティ運営。タイル化されたグラフが特徴 | Java。GraphHopper GmbH が開発。柔軟なコスト定義が特徴 |
| 探索が走る場所 | API プロセスのメモリ（CSR 常駐） | DB サーバー（クエリごとに SQL の結果からグラフを組む） | 専用プロセスのメモリ | 専用プロセス。タイルをディスクから必要分だけ読む（メモリが小さい） | 専用プロセスのメモリ（JVM） |
| 前処理 | なし（起動時に CSR 構築 1.3 秒） | なし（テーブルに入れるだけ・pgr_createTopology） | **重い**。extract → CH（contract）or MLD（partition + customize）。全国 388 万リンクで extract 30 秒・MLD 37 秒・CH 212 秒 | `valhalla_build_tiles`（階層タイル化） | 任意。CH / Landmark を作れば速く、なしでも動く（flexible） |
| 入力 | 何でも（parquet を直接） | テーブル（id, source, target, cost） | OSM PBF + Lua プロファイル（タグ → 速度） | OSM PBF + costing 設定 | OSM PBF + カスタムモデル（JSON で速度・優先度） |
| 到達圏 | Dijkstra の距離配列を**リンク単位**でそのまま返せる | `pgr_drivingDistance`（ノード単位） | **API なし**（`/table` の多対多で代替） | `/isochrone`（等時間線ポリゴン）。`/expansion` で辺単位（デバッグ用途） | `/isochrone`（ポリゴン） |
| 経路 | 素の Dijkstra。全国で 280 ms | `pgr_dijkstra`。全国で 3.3 秒 | **1〜5 ms**（CH）。世界規模を想定 | 数十 ms 級（階層探索・動的コスト） | 数 ms 級（CH）〜数百 ms（flexible） |
| 更新 | 再起動（7 秒） | `UPDATE` 即反映 | 再前処理（分〜時間） | 再タイル化 | 再インポート（CH なしなら軽い） |
| 得意な場面 | 自前データ・独自の出力形式・探索の中身を触りたい | 市区町村〜都市圏で SQL に閉じたい・データが頻繁に変わる | 大陸規模の経路・距離行列・マップマッチング | メモリの小さい環境・リクエストごとにコストを変える・等時間線 | 独自のコスト定義・Java 環境・Web UI 込みで手早く |
| 本プロジェクトとの相性 | ◎ | △（規模） | 経路 ◎・到達圏 ✕（リンク単位を返せない）・入力の変換が必要 | 経路 ○・到達圏はポリゴン・入力の変換が必要 | 経路 ○・到達圏はポリゴン・入力の変換が必要 |


## 2. 方法

同じ 388 万リンク・同じ速度テーブル（高速 80 / 国道 35 / 都道府県道 30 / 市区町村道 20 km/h）を使う。

| 項目 | 内容 |
|---|---|
| 入力の変換 | `preprocess/export_osm_pbf.py`（`uv run --with osmium`）。parquet → OSM PBF。交差点ノードは node_id をそのまま OSM id、リンク形状の中間頂点は 10^12 + 連番、way id = link_id + 1。tags: `highway`（階層分け用）, `maxspeed`（速度）, `oneway=no`, `ksj:class`, `ksj:link_id`。**36 秒・112 MB**。端点の不一致 0 |
| OSRM | `ghcr.io/project-osrm/osrm-backend`。`bench/osrm/ksj.lua`（速度 = maxspeed・ターンペナルティ 0・無向）。extract → partition + customize（MLD）と contract（CH）の両方 |
| GraphHopper | `israelhikingmap/graphhopper`（12.0-SNAPSHOT・Java 21）。`bench/graphhopper/config.yml`（カスタムモデル `limit_to: max_speed`・CH あり・RAM_STORE） |
| Valhalla | `ghcr.io/valhalla/valhalla`（3.8.3）。`valhalla_build_config` → `valhalla_build_tiles`。auto costing（maxspeed を速度に使う） |
| scipy | 手元の API（`uv run uvicorn`・ホスト）。`/route` `/reachability` を HTTP で叩く |
| 計測 | Mac（Apple Silicon・RAM 32 GB）。専用エンジンは Docker Desktop（VM 7.7 GB）上。HTTP 往復の中央値（5 回、到達圏は 3 回）。scipy は「内部 ms」（探索だけ）も併記 |
| 注意 | 同時に他のコンテナ（DB・GraphHopper 2.9 GB など）が動いた状態で測った。専用エンジン同士の数 ms の差は Docker の HTTP 経由なので参考値 |

**変換で踏んだこと**: Valhalla はノードが id 昇順でないと `Detected unsorted input data` で落ちる（交差点ノードを argsort で書く）。
また trunk / primary は最上位階層（4° タイル）に入るため、国道 + 都道府県道を全部そこに置くと `Exceeded maximum edgeinfo offset`（1 タイル 32 MB 上限）で落ちた。国道 → secondary、県道 → tertiary に下げて解決（速度は maxspeed なので結果に影響しない）。
OSRM と GraphHopper は最初の割り当て（trunk / primary）で構築したが、どちらも速度は maxspeed から取るので同じ。

## 3. 結果: 前処理・常駐メモリ

| | scipy CSR | OSRM MLD | OSRM CH | GraphHopper CH | Valhalla |
|---|---|---|---|---|---|
| 前処理 | なし（起動時 CSR 構築 1.3 秒・全体 5〜7 秒） | extract 30 秒 + partition 34 秒 + customize 3 秒 | extract 30 秒 + contract **212 秒** | import + CH **70 秒**（CH 36 秒） | build_tiles **117 秒**（1 回目は階層の詰め過ぎで 139 秒後に失敗） |
| 前処理ピーク RAM | — | extract 5.9 GB / partition 3.0 GB | extract 5.9 GB / contract 2.5 GB | 1.7 GB（-Xmx3g） | 未計測（コンテナ 0.5 GB 前後で推移） |
| 生成物 | — | 2.1 GB（.osrm.*） | + hsgr 0.5 GB | 331 MB | 787 MB（tiles/） |
| 常駐メモリ | 2.9〜3.1 GB | 1.85 GB | 1.85 GB | 0.7 GB（起動時）→ 2.95 GB（到達圏後） | 0.06 GB（起動時）→ 2.5 GB（到達圏後・タイルをキャッシュ） |
| グラフの整合 | 388 万リンク・371 万ノード・成分 28,817 | 強連結成分 **28,817**（scipy と一致） | 同左 | edges **3,881,601**・nodes **3,710,962**（scipy と一致） | — |

## 4. 結果: 経路（HTTP 往復の中央値）

| 区間 | scipy（内部 / HTTP） | OSRM MLD | OSRM CH | GraphHopper CH | Valhalla |
|---|---|---|---|---|---|
| 東京駅 → 横浜駅（37 km） | 311 / 283 ms | 2.8 ms | **1.0 ms** | 16.3 ms | 10.7 ms |
| 東京駅 → 仙台駅（350 km） | 275 / 284 ms | 7.3 ms | **3.0 ms** | 18.3 ms | 15.6 ms |
| 東京駅 → 大阪駅（500 km） | 280 / 304 ms | 4.7 ms | **2.2 ms** | 22.3 ms | 17.8 ms |
| 東京駅 → 鹿児島中央（1,357 km） | 281 / 322 ms | 11.4 ms | **4.8 ms** | 34.3 ms | 24.9 ms |
| 札幌駅 → 鹿児島中央 | 到達不能 | 到達不能（400） | 到達不能（400） | 到達不能（400） | 到達不能（400） |

距離・所要は 4 エンジンで **1% 以内**で一致（東京→大阪: scipy 500.4 km / 379.6 分、OSRM 506.1 km / 383.6 分、GraphHopper 501.5 km / 380.0 分、Valhalla 505.0 km / 382.3 分）。
差は OSRM が座標から距離を計算し直すこと、重みの丸めによる。北海道は本州と道路が繋がっていないので全員が到達不能で整合。

scipy の 280 ms は前処理なしの素の Dijkstra が全国グラフを走る時間で、区間の長さにほとんど依らない（近くても遠くても 280 ms）。
専用エンジンは CH / MLD の階層を使うので距離に応じて 1 → 5 ms と伸びるだけ。**経路だけなら専用エンジンは 2 桁速い。**

## 5. 結果: 到達圏（東京駅起点）

| 打ち切り | scipy `/reachability`（リンク単位） | GraphHopper `/isochrone`（ポリゴン） | Valhalla `/isochrone`（ポリゴン） | Valhalla `/expansion`（辺単位） | OSRM |
|---|---|---|---|---|---|
| 30 分 | 135,269 本・内部 109 ms・HTTP 124 ms | 頂点 8,579・**4,269 ms** | 頂点 2,098・**111 ms** | 辺 247,696（有向）・36 MB・326 ms | API なし |
| 120 分 | 786,335 本・内部 94 ms・HTTP 712 ms（gzip 3.6 MB） | 頂点 11,628・**16,846 ms** | 頂点 3,696・**367 ms** | 辺 1,546,129・226 MB・1,707 ms | API なし |
| 480 分 | 326,406 本（auto で幹線に絞る）・内部 290 ms | **拒否**（"Too many nodes would be included in post processing (3,377,213)"） | 頂点 10,869・**1,657 ms** | — | API なし |

到達圏は様相が逆転する。専用エンジンの等時間線は「Dijkstra で全ノードの到達時間を出す → 点群からポリゴンを組む」の後処理が重く、GraphHopper は 30 分で 4 秒、120 分で 17 秒、480 分は上限で拒否。
Valhalla はグリッド法で等時間線を組むので速い（30 分 111 ms・480 分 1.7 秒）が、出力はやはりポリゴン。
Valhalla の `/expansion` だけが辺単位を返せる（探索で触った有向辺を GeoJSON で全部吐く）。ただし 30 分で 36 MB・120 分で 226 MB の非圧縮 GeoJSON になり、
形状を毎回運ぶので scipy の `link_id[]` + `cost[]`（120 分で gzip 3.6 MB・形はタイルに一度だけ）とは設計思想が違う。名前どおりデバッグ用途。
scipy は Dijkstra の距離配列をリンク番号で返すだけなので 100〜300 ms で、しかもこのアプリが要る出力（リンクごとの到達時間 → タイルの着色）そのもの。
専用エンジンのポリゴンは「30 分圏の外形」を見せる用途には向くが、道路 1 本ずつを塗る用途には使えない。

## 5-2. まとめ（1 枚）

| 観点 | 勝者 | 差 |
|---|---|---|
| 経路（全国 1 本） | OSRM CH（1〜5 ms）> Valhalla（11〜25 ms）≒ GraphHopper（16〜34 ms）≫ scipy（280 ms）≫ pgRouting（3.3 秒） | 前処理の深さの順 |
| 到達圏（リンク単位の時間） | **scipy**（100〜300 ms・そのまま着色に使える）。Valhalla `/expansion` は出せるが 36〜226 MB。他は出せない | 出力形式の差 |
| 到達圏（外形ポリゴン） | Valhalla（0.1〜1.7 秒）> GraphHopper（4〜17 秒・480 分は拒否） | scipy は作っていない（不要） |
| 前処理 | scipy・pgRouting なし。GraphHopper 70 秒 < Valhalla 117 秒 < OSRM CH 242 秒 | データ更新の重さに直結 |
| 入力 | scipy・pgRouting は自前データ直結。専用 3 つは OSM PBF への変換層（36 秒・ただし階層や並び順の癖を 2 回踏んだ） | |
| 常駐メモリ | Valhalla 0.06 → 2.5 GB、OSRM 1.85 GB、GraphHopper 0.7 → 2.95 GB、scipy 3.0 GB | 同じ桁 |

## 6. 位置づけ

3 つとも OSM 用の C++/Java 製で、**scipy と同じ「前処理してメモリに常駐」の系統**。違いは前処理の深さで、OSRM は Contraction Hierarchies / MLD、
Valhalla は階層タイル化、GraphHopper は CH / Landmark を組み、大陸規模でも経路を数 ms で返す。scipy の 267 ms（東京→大阪）は前処理なしの素の Dijkstra の数字なので、
**経路だけを見れば専用エンジンの方が 1〜2 桁速い**。本プロジェクトで採らなかった理由は 3 つ。

| 論点 | 専用エンジン | 本プロジェクト（scipy） |
|---|---|---|
| 入力データ | OSM PBF 前提。N13 を OSM 形式に変換する層を自作する必要（1〜2 日）。道路分類 → 速度のモデルも Lua プロファイル（OSRM）や costing（Valhalla）に書き直す | GeoParquet を直接読む。速度は前処理の `SPEED_KMH` 1 行 |
| 到達圏の出力 | Valhalla `/isochrone` は等時間線のポリゴン、OSRM に到達圏 API はない（`/table` の多対多で代替）。**リンクごとの到達コスト**（ADR-2 の `link_id[]` + `cost[]`）を返す API は標準にない（Valhalla の `/expansion` はデバッグ用途で近い） | Dijkstra の距離配列をそのまま返せる。タイルの feature-state 着色と直結 |
| 探索の理解 | 探索がブラックボックスになり、3 層（SvelteKit / FastAPI / PostgreSQL）の中で FastAPI の役割がプロキシに縮む | 探索の中身（CSR・並行リンクの畳み込み・スナップ・孤立成分）を自分で触れる |

**帰結**: 経路探索だけの製品なら専用エンジンが標準解で、scipy を選ぶ理由はない。本プロジェクトは「自前データ × リンク単位の到達圏 × 探索の理解」の 3 点で
専用エンジンの得意領域から外れており、scipy in-memory が最短だった。サービス化で経路の速度が問題になったら、scipy に CH を足すより OSRM/Valhalla に載せ替える方が早い。
pgRouting との対比は「DB で探索するか、メモリで探索するか」、専用エンジンとの対比は「前処理をどこまで深くするか」で、軸が違う。


## 7. 再現手順

```bash
# 変換（36 秒）
uv run --with osmium python preprocess/export_osm_pbf.py       # → network/nationwide/roads_nationwide.osm.pbf

# OSRM（bench/osrm）
cp network/nationwide/roads_nationwide.osm.pbf bench/osrm/roads.osm.pbf && cd bench/osrm
docker run --rm -t -v "$PWD:/data" ghcr.io/project-osrm/osrm-backend osrm-extract -p /data/ksj.lua /data/roads.osm.pbf
docker run --rm -t -v "$PWD:/data" ghcr.io/project-osrm/osrm-backend osrm-partition /data/roads.osrm
docker run --rm -t -v "$PWD:/data" ghcr.io/project-osrm/osrm-backend osrm-customize /data/roads.osrm
docker run --rm -t -v "$PWD:/data" ghcr.io/project-osrm/osrm-backend osrm-contract  /data/roads.osrm     # CH（212 秒）
docker run -d --name osrm-ch -p 5002:5000 -v "$PWD:/data" ghcr.io/project-osrm/osrm-backend osrm-routed --algorithm ch /data/roads.osrm
curl 'localhost:5002/route/v1/driving/139.7671,35.6812;135.4959,34.7025?overview=false'

# GraphHopper（bench/graphhopper）
docker run --rm -v "$PWD:/data" -e JAVA_OPTS="-Xmx3g -Xms1g" israelhikingmap/graphhopper --import -c /data/config.yml -i /data/roads.osm.pbf -o /data/graph-cache
docker run -d --name gh -p 8989:8989 -v "$PWD:/data" -e JAVA_OPTS="-Xmx3g -Xms1g" israelhikingmap/graphhopper -c /data/config.yml -i /data/roads.osm.pbf -o /data/graph-cache
curl 'localhost:8989/route?point=35.6812,139.7671&point=34.7025,135.4959&profile=car&calc_points=false'
curl 'localhost:8989/isochrone?point=35.6812,139.7671&profile=car&time_limit=1800&buckets=1'

# Valhalla（bench/valhalla）
docker run --rm -v "$PWD:/data" ghcr.io/valhalla/valhalla valhalla_build_config --mjolnir-tile-dir /data/tiles --mjolnir-tile-extract /data/tiles.tar \
  --mjolnir-timezone /data/tiles/timezones.sqlite --mjolnir-admin /data/tiles/admins.sqlite --service-limits-isochrone-max-time-contour 1920 \
  --service-limits-isochrone-max-distance 2000000 --service-limits-auto-max-distance 3000000 --mjolnir-concurrency 4 > valhalla.json
docker run --rm -v "$PWD:/data" ghcr.io/valhalla/valhalla valhalla_build_tiles -c /data/valhalla.json /data/roads.osm.pbf
docker run -d --name valhalla -p 8002:8002 -v "$PWD:/data" ghcr.io/valhalla/valhalla valhalla_service /data/valhalla.json 2
curl -X POST localhost:8002/route -d '{"locations":[{"lat":35.6812,"lon":139.7671},{"lat":34.7025,"lon":135.4959}],"costing":"auto","units":"kilometers"}'
curl -X POST localhost:8002/isochrone -d '{"locations":[{"lat":35.6812,"lon":139.7671}],"costing":"auto","contours":[{"time":30}],"polygons":true}'
```

生成物（PBF・.osrm.*・tiles・graph-cache、合わせて数 GB）は `.gitignore` で除外。設定ファイル（`ksj.lua` `config.yml` `valhalla.json`）だけ管理する。


## 8. 同じ始点・終点で同じ経路になるか（東京 23 区・2026-10-05）

**きっかけ**: 「GraphHopper と Valhalla を比べると、同じ始点・終点でもルート選定がわりと違う。言語が違うし、探索ライブラリも似て非なるのか」という意見。
同じネットワーク・同じ速度なら同じ経路になるはずか、違うならアルゴリズムのせいか、を確かめた。scipy は比べず、専用エンジン 3 つどうしで見る。

### 8-1. 方法

| 項目 | 内容 |
|---|---|
| 範囲 | 東京 23 区を覆う矩形（東経 139.56〜139.92・北緯 35.52〜35.82）。両端の交差点ノードが矩形に入るリンクだけ。**106,135 リンク・98,366 ノード**（`preprocess/clip_network.py`） |
| エンジン | 2 章と同じ設定（速度 = maxspeed・無向・ターンペナルティなし）。OSRM MLD・GraphHopper CH・Valhalla auto（既定のまま） |
| 始点・終点 | 最寄りの KSJ 交差点ノードに寄せ、その座標を 3 エンジンに渡す（エンジンごとの吸着の違いを消す） |
| 比べ方 | 各エンジンの経路の形を KSJ のリンク列に戻し、**リンク列が完全一致するか**で判定。違うときは、リンク列に KSJ の所要（`time_001min`）を足し直した「KSJ 換算」の所要で、所要が同じ別経路か、所要も違うかを分ける |
| 組 | ランダム 100 組（スナップ対象ノードから一様・seed=1）。ビューワの「まとめて比べる」と同じ |
| 環境 | Windows 11・Docker Desktop。前処理は数秒〜数十秒（Valhalla の build_tiles だけ 149 秒） |

ビューワ（`bench/compare/`）で地図上の任意の 2 地点も比べられる。3 本の経路を色分けして重ね、組み合わせごとの一致・KSJ 換算の差・重なり（共通リンクの延長 ÷ 和集合の延長）を出す。

**経路の形 → リンク列の照合で踏んだこと**（素朴に「頂点に最も近い交差点ノードを拾う」と、OSRM でも 50 組で 324 か所繋がらなかった）:

- KSJ はメッシュ境界などで **3 m 以内に別のノード**を置き、**長さ 1 m のリンク**で結んでいる（23 区で 353 組）。最寄りの 1 つだけ取ると繋がらない方を拾う
- 高架・地下のリンクは、下（上）の道路の**交差点ノードの真上**を通る。その交差点からの取り付け道路も実在するので、「リンクが在るか」だけで判定すると寄り道の並びを拾う
- 首都高の高架の下を一般道が並走すると、一般道の短いリンクが高架の形に沿って連なる。「繋がった数」で数えると、高速の長いリンク 1 本より一般道の並びが勝つ

→ 頂点ごとに 1.5 m 以内の候補ノードを全部挙げ、「選ぶ／読み飛ばす」の並びのうち（繋がった延長 − 繋がらなかった延長）が最大のものを動的計画法で選ぶ。
「繋がった」は、2 ノードを結ぶリンクがあり、その長さが経路の形に沿った区間長と 5 m・3% 以内で合うこと。同じ頂点の候補どうし（1 m のリンク）も渡れるようにした。
これで 100 組の取りこぼしは OSRM 1・GraphHopper 1・Valhalla 0 か所（`bench/compare/server.py` の `to_links`）。

**GraphHopper の経路の形の間引き**: 既定で 1 m の Douglas-Peucker が掛かり、直進する交差点の頂点が消えてリンク列に戻せない。
設定ファイルの `routing.way_point_max_distance: 0` は効かず（12.0-SNAPSHOT）、リクエストに `way_point_max_distance=0` を付けて止めた。

### 8-2. 結果: 3 エンジンどうし（100 組）

| 組み合わせ | 同じ経路 | 違う・所要は同じ | 違う・所要も違う | 所要の差 平均（最大） | 重なり平均 |
|---|---|---|---|---|---|
| OSRM – GraphHopper | **63** | 14 | 23 | 1 秒（5 秒） | 95% |
| OSRM – Valhalla | 18 | 9 | 73 | 25 秒（140 秒） | 71% |
| GraphHopper – Valhalla | 17 | 8 | 75 | 24 秒（140 秒） | 71% |

3 つとも同じ経路は **15 組**。OSRM と GraphHopper は 77 組で所要まで一致し、残りも数秒差。外れるのは Valhalla が絡む組み合わせ。

### 8-3. 結果: アルゴリズムのせいか（`bench/compare/algo_test.py`）

**同じエンジン・同じ重みで、アルゴリズムだけ替える**

| 比較 | 同じ | 違う・所要同じ | 違う・所要も違う | 最大差 |
|---|---|---|---|---|
| GraphHopper CH ↔ ダイクストラ法（`ch.disable=true&algorithm=dijkstra`） | 89 | 2 | 9 | 1.2 秒 |
| OSRM MLD ↔ CH | 85 | 3 | 12 | 1.2 秒 |
| （参考）OSRM MLD ↔ GraphHopper CH | 63 | 14 | 23 | 5.4 秒 |

CH・MLD は前処理で近道（ショートカット）や区画の表を足したグラフの上でダイクストラ法を走らせる手法で、答えのコストはダイクストラ法と同じになる（厳密解）。
アルゴリズムを替えても、変わるのは所要の差 1 秒前後の組だけ（近道の重みの丸めと、所要が同じ経路のどれを選ぶか）。
**OSRM と GraphHopper の差（最大 5 秒）はアルゴリズムではなく重みの細部**（距離を座標から計算し直す・丸め）による。言語（C++ / Java）は関係ない。

**Valhalla を Valhalla 自身の物差しで測り直す**

Valhalla と OSRM で経路が違う 82 組について、OSRM の経路を Valhalla の道路網上でなぞり（`/trace_route`・`shape_match: map_snap`）、Valhalla の所要で測った。
Valhalla 自身の経路をなぞると `/route` の所要と誤差 0 秒で一致するので、測り方は正確。

| | OSRM の経路の方が Valhalla の物差しで速い | うち 3 秒超 | 最大 |
|---|---|---|---|
| Valhalla 既定（階層の打ち切りあり） | **47 / 82 組** | 34 組 | 111 秒 |
| 打ち切りなし（`hierarchy_limits` を全部外す） | 44 / 82 組 | 30 組 | 146 秒 |

Valhalla は**自分の物差しでも最短でない経路を返す**。所要の計算方法の違いでは説明できず、探索の近似による。
既定の `hierarchy_limits` は、生活道路（階層 2）を始点・終点から 5 km 以内、県道クラス（階層 1）を 20 km 以内でしか探索しない。外すと 14 組の経路が変わり最大 76 秒縮むが、それでも 44 組は OSRM の経路の方が速い。
残りは双方向 A\* が厳密な最適を待たずに打ち切ることと、タイルに作る近道（`shortcuts`）によると見ているが、確かめていない。

**Valhalla の所要時間の計算方法**: auto の既定のまま使った。50 組で試すと、ペナルティ系（`maneuver_penalty`・`gate_cost` など）を 0 にしても経路は変わらず、
`use_highways` は既定の 0.5 が中立（1 で高速を好み遠回り、0.25 以下で高速を避けて大きく外れる）。Valhalla は右左折のコストを内部で足すので、同じ経路でも所要が他より長い（東京駅 → 新宿駅 で 10.2 分。OSRM 9.85 分・GraphHopper 9.79 分）。この加算はオプションで 0 にできない。

### 8-4. まとめ

| 違いの原因 | OSRM と GraphHopper | Valhalla |
|---|---|---|
| アルゴリズム（厳密か近似か） | 影響なし（CH・MLD は厳密） | **影響あり**（階層の打ち切り・双方向 A\* の打ち切り） |
| 所要時間の計算方法 | 揃えた（差は数秒） | 右左折のコストを内部で足す（0 にできない） |
| 所要が同じ経路のどれを選ぶか | 違いの大半はこれ | あり |

「言語が違うから」ではない。「探索が似て非なる」は Valhalla については当たっていて、理由は「ダイクストラ法の実装が違う」ではなく「Valhalla は速さのために厳密さを手放している」こと。
同じ C++ 製の OSRM と Valhalla を比べても、この差は出る。経路の一致を求める比較や検証には、厳密解の OSRM・GraphHopper（または scipy）を使う。

### 8-5. 再現手順

```bash
bash bench/compare/build.sh                                    # 切り出し → PBF → 3 エンジンの前処理と起動（docker compose）
uv run uvicorn bench.compare.server:app --port 8010            # ビューワ → http://localhost:8010/
uv run python -m bench.compare.algo_test                       # 8-3 の実験（追加で立てるコンテナはスクリプト冒頭の docstring）
```

生成物（`bench/compare/data/`・`network/tokyo23/`）は `.gitignore` で除外。
