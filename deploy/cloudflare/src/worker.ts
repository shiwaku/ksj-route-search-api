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
const TILE_KEY = "roads_nationwide.pmtiles";

const json = (status: number, detail: string, headers: HeadersInit = {}) =>
  Response.json({ detail }, { status, headers });

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/")) return api(request, url, env);
    if (url.pathname === TILE_PATH) return tiles(request, env);
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

async function tiles(request: Request, env: Env): Promise<Response> {
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
  // タイルは国土数値情報の年次更新でしか変わらない。変えるときはファイル名ごと変える
  headers.set("Cache-Control", "public, max-age=86400");
  return headers;
}
