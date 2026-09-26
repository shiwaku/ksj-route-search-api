// ブラウザ → この Worker → パスで振り分け（deploy-cloudflare.md 3 章）
//   /api/*   → コンテナ（FastAPI）。/api を剥がして渡す。重い探索は IP ごとに回数制限
//   /tiles/* → R2 の PMTiles。Range をそのまま R2 に渡して 206 で返す
//   それ以外 → Static Assets（wrangler.jsonc の run_worker_first に無いパスはここに来ない）
import { Container, getContainer } from "@cloudflare/containers";

export class RouteContainer extends Container {
  defaultPort = 8000;
  // 最後のリクエストからこの時間で寝る。寝ている間は課金されず、次のリクエストで起きる
  sleepAfter = "10m";
  // グラフはイメージに焼いてあり、外に取りに行くものはない
  enableInternet = false;
}

interface Env {
  ROUTE_CONTAINER: DurableObjectNamespace<RouteContainer>;
  TILES: R2Bucket;
  ASSETS: Fetcher;
  API_LIMITER: RateLimit;
}

// 公開するのは探索系だけ。/bookmarks /bench はコンテナ側でも登録されない（DATABASE_URL なし）
const API_PATHS = new Set(["/health", "/reachability", "/route"]);
const LIMITED = new Set(["/reachability", "/route"]);
const TILE_PATH = "/tiles/roads_nationwide.pmtiles";
// 共有バケット shi-works のキー。第 1 階層はアクセス方法（pmtiles/）、第 2 階層はプロジェクト名
// （xserver-cleanup の R2-STRUCTURE.md §4）。ファイル名の版（N13-24）はグラフ parquet と揃え、
// 同じデータでタイルの作り方だけ変えたら _v2, _v3 … を付ける（_v2: z5-8 を高速・国道だけにした・issue #17）。
// データを更新するときは新しい版を別キーで置き、ここを書き換えてイメージと同時にデプロイする（README「データの更新」）
const TILE_KEY = "pmtiles/ksj-route-search/roads_nationwide_N13-24_v2.pmtiles";

const json = (status: number, detail: string, headers: HeadersInit = {}) =>
  Response.json({ detail }, { status, headers });

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/")) return api(request, url, env);
    if (url.pathname === TILE_PATH) return tiles(request, env, ctx);
    if (url.pathname.startsWith("/tiles/")) return new Response("Not found", { status: 404 });
    return env.ASSETS.fetch(request);
  },
};

async function api(request: Request, url: URL, env: Env): Promise<Response> {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return json(405, "GET のみ受け付けます", { Allow: "GET, HEAD" });
  }
  const path = url.pathname.slice("/api".length);
  if (!API_PATHS.has(path)) return json(404, "Not Found");
  if (LIMITED.has(path)) {
    const key = request.headers.get("CF-Connecting-IP") ?? "unknown";
    const { success } = await env.API_LIMITER.limit({ key });
    if (!success) {
      return json(429, "リクエストが多すぎます。1 分ほど待ってからもう一度お試しください", { "Retry-After": "60" });
    }
  }
  url.pathname = path;
  // インスタンス名を固定して 1 台に寄せる（グラフはそのコンテナのメモリ上にある）
  return getContainer(env.ROUTE_CONTAINER, "main").fetch(new Request(url, request));
}

// R2 から毎回読むと Range 1 回に約 0.2 秒掛かり、地図 1 画面で数十回読むので描画が遅い（issue #4）。
// 読んだ範囲をこのデータセンターの Cache API に置き、2 回目以降はそこから返す。Cache API は無料。
// cache.put は 206 を受け付けないので「URL＋範囲」をキーに 200 で保存し、返すときに 206 に戻す。
// キーに TILE_KEY（版入り）を含めるので、データ更新でキーを変えれば古いキャッシュは読まれない
const TILE_CACHE_SECONDS = 86400;
// PMTiles のクライアントが読むのはディレクトリ（16 KB 程度）とタイル（上限 1.5 MB）。これより大きい範囲は置かない
const TILE_CACHE_MAX_BYTES = 4 * 1024 * 1024;

async function tiles(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  const m = request.method === "GET" ? /^bytes=(\d+)-(\d+)$/.exec(request.headers.get("Range") ?? "") : null;
  if (m && !request.headers.has("If-None-Match")) {
    const start = Number(m[1]), end = Number(m[2]);
    if (end >= start && end - start + 1 <= TILE_CACHE_MAX_BYTES) {
      return cachedRange(request, env, ctx, start, end);
    }
  }
  return tilesFromR2(request, env);
}

async function cachedRange(request: Request, env: Env, ctx: ExecutionContext, start: number, end: number): Promise<Response> {
  const keyUrl = new URL(request.url);
  keyUrl.pathname = `/__tile-cache/${TILE_KEY}`;
  keyUrl.search = `?r=${start}-${end}`;
  const key = new Request(keyUrl.toString());
  const cache = caches.default;

  const hit = await cache.match(key);
  if (hit) {
    const etag = hit.headers.get("ETag");
    const ifMatch = request.headers.get("If-Match");
    if (ifMatch && etag && ifMatch !== etag) return new Response(null, { status: 412 });
    return rangeResponse(hit.body, hit.headers, "HIT");
  }

  const obj = await env.TILES.get(TILE_KEY, { range: { offset: start, length: end - start + 1 }, onlyIf: request.headers });
  if (!obj) return new Response("Not found", { status: 404 });
  const headers = tileHeaders(obj);
  if (!("body" in obj)) return new Response(null, { status: 412, headers });
  const last = Math.min(end, obj.size - 1);
  headers.set("Content-Range", `bytes ${start}-${last}/${obj.size}`);
  const buf = await obj.arrayBuffer();

  const stored = new Headers(headers);
  stored.set("Cache-Control", `public, max-age=${TILE_CACHE_SECONDS}`);
  ctx.waitUntil(cache.put(key, new Response(buf, { status: 200, headers: stored })));
  return rangeResponse(buf, headers, "MISS");
}

function rangeResponse(body: BodyInit | null, src: Headers, cacheStatus: "HIT" | "MISS"): Response {
  const headers = new Headers(src);
  // ブラウザ向けは R2 のオブジェクトと同じ 1 時間（キャッシュに置く側の 1 日とは別）
  headers.set("Cache-Control", "public, max-age=3600");
  headers.set("X-Tile-Cache", cacheStatus);
  headers.delete("Content-Length");
  return new Response(body, { status: 206, headers });
}

async function tilesFromR2(request: Request, env: Env): Promise<Response> {
  if (request.method === "HEAD") {
    const head = await env.TILES.head(TILE_KEY);
    if (!head) return new Response("Not found", { status: 404 });
    const headers = tileHeaders(head);
    headers.set("Content-Length", String(head.size));
    return new Response(null, { headers });
  }
  if (request.method !== "GET") return new Response("Method not allowed", { status: 405 });

  const obj = await env.TILES.get(TILE_KEY, { range: request.headers, onlyIf: request.headers });
  if (!obj) return new Response("Not found", { status: 404 });
  const headers = tileHeaders(obj);
  // 条件付きリクエストが外れると body の無い R2Object が返る
  if (!("body" in obj)) {
    return new Response(null, { status: request.headers.has("If-None-Match") ? 304 : 412, headers });
  }
  const r = obj.range as { offset?: number; length?: number; suffix?: number } | undefined;
  if (!request.headers.has("Range") || !r) {
    return new Response(obj.body, { headers });
  }
  const start = r.suffix !== undefined ? obj.size - r.suffix : (r.offset ?? 0);
  const length = r.suffix !== undefined ? r.suffix : (r.length ?? obj.size - start);
  headers.set("Content-Range", `bytes ${start}-${start + length - 1}/${obj.size}`);
  headers.set("Content-Length", String(length));
  return new Response(obj.body, { status: 206, headers });
}

function tileHeaders(obj: R2Object): Headers {
  const headers = new Headers();
  obj.writeHttpMetadata(headers);
  headers.set("ETag", obj.httpEtag);
  headers.set("Accept-Ranges", "bytes");
  headers.set("Content-Type", "application/octet-stream");
  // shi-works バケットの規約（R2-STRUCTURE.md §6.6）に合わせる。版を変えると ETag が変わる
  headers.set("Cache-Control", "public, max-age=3600");
  return headers;
}
