/**
 * 道路ネットワーク Dijkstra Web Worker
 *
 * メッセージプロトコル:
 *   受信: { type: 'reachability', lat, lon, max_min, mode }
 *         { type: 'route', orig_lat, orig_lon, dest_lat, dest_lon, mode }
 *   送信: { type: 'ready', nLinks, nNodes }
 *         { type: 'reachability_result', links, meta }
 *         { type: 'route_result', link_ids, meta }
 *         { type: 'error', message }
 */

let nLinks, nNodes;
let node1, node2, costV, costW, linkId, nodeLat, nodeLon;
let ptr, edgeDst, edgeCosV, edgeCosW, edgeLid;  // CSR

// ─────────────────────────────────────────────
// net.bin ロード・パース
// ─────────────────────────────────────────────
async function loadGraph(binUrl) {
  const res = await fetch(binUrl);
  if (!res.ok) throw new Error(`net.bin fetch failed: ${res.status}`);
  const buf = await res.arrayBuffer();

  const view = new DataView(buf);
  nLinks = view.getUint32(0, true);
  nNodes = view.getUint32(4, true);

  let off = 8;
  node1   = new Uint32Array(buf, off, nLinks); off += nLinks * 4;
  node2   = new Uint32Array(buf, off, nLinks); off += nLinks * 4;
  costV   = new Float32Array(buf, off, nLinks); off += nLinks * 4;
  costW   = new Float32Array(buf, off, nLinks); off += nLinks * 4;
  linkId  = new Uint32Array(buf, off, nLinks); off += nLinks * 4;
  nodeLat = new Float32Array(buf, off, nNodes); off += nNodes * 4;
  nodeLon = new Float32Array(buf, off, nNodes);

  buildCSR();
}

// ─────────────────────────────────────────────
// CSR グラフ構築（双方向）
// ─────────────────────────────────────────────
function buildCSR() {
  const deg = new Uint32Array(nNodes);
  for (let i = 0; i < nLinks; i++) {
    deg[node1[i]]++;
    deg[node2[i]]++;
  }

  ptr = new Uint32Array(nNodes + 1);
  for (let i = 0; i < nNodes; i++) ptr[i + 1] = ptr[i] + deg[i];

  const ne = 2 * nLinks;
  edgeDst  = new Uint32Array(ne);
  edgeCosV = new Float32Array(ne);
  edgeCosW = new Float32Array(ne);
  edgeLid  = new Uint32Array(ne);

  const pos = new Uint32Array(nNodes);
  for (let i = 0; i < nLinks; i++) {
    const u = node1[i], v = node2[i];
    const cv = costV[i], cw = costW[i], lid = linkId[i];

    let p = ptr[u] + pos[u]++;
    edgeDst[p] = v; edgeCosV[p] = cv; edgeCosW[p] = cw; edgeLid[p] = lid;

    p = ptr[v] + pos[v]++;
    edgeDst[p] = u; edgeCosV[p] = cv; edgeCosW[p] = cw; edgeLid[p] = lid;
  }
}

// ─────────────────────────────────────────────
// 最近傍ノード検索（線形探索・706k で ~5ms）
// ─────────────────────────────────────────────
function nearestNode(lat, lon) {
  let bestDist = Infinity, bestIdx = 0;
  for (let i = 0; i < nNodes; i++) {
    const dlat = nodeLat[i] - lat;
    const dlon = nodeLon[i] - lon;
    const d = dlat * dlat + dlon * dlon;
    if (d < bestDist) { bestDist = d; bestIdx = i; }
  }
  const snapM = Math.sqrt(bestDist) * 111000;
  return { idx: bestIdx, snapM };
}

// ─────────────────────────────────────────────
// バイナリ最小ヒープ
// ─────────────────────────────────────────────
class MinHeap {
  constructor() { this.h = []; }
  get size() { return this.h.length; }
  push(cost, node) {
    this.h.push(cost, node);
    this._up(this.h.length / 2 - 1);
  }
  pop() {
    const c = this.h[0], n = this.h[1];
    const last = this.h.length - 2;
    if (last > 0) {
      this.h[0] = this.h[last]; this.h[1] = this.h[last + 1];
      this.h.length = last;
      this._down(0);
    } else {
      this.h.length = 0;
    }
    return { cost: c, node: n };
  }
  _up(i) {
    const h = this.h;
    while (i > 0) {
      const p = ((i - 1) >> 1) & ~0;  // parent (even index)
      if (h[p] <= h[i]) break;
      [h[p], h[i], h[p+1], h[i+1]] = [h[i], h[p], h[i+1], h[p+1]];
      i = p;
    }
  }
  _down(i) {
    const h = this.h, n = h.length;
    while (true) {
      let m = i;
      const l = i + 2, r = i + 4;
      if (l < n && h[l] < h[m]) m = l;
      if (r < n && h[r] < h[m]) m = r;
      if (m === i) break;
      [h[m], h[i], h[m+1], h[i+1]] = [h[i], h[m], h[i+1], h[m+1]];
      i = m;
    }
  }
}

// ─────────────────────────────────────────────
// 到達圏 Dijkstra（1対N・limit付き）
// ─────────────────────────────────────────────
function dijkstraReach(srcIdx, maxMin, mode) {
  const costs = mode === 'vehicle' ? edgeCosV : edgeCosW;
  const dist  = new Float32Array(nNodes).fill(Infinity);
  dist[srcIdx] = 0;

  const heap = new MinHeap();
  heap.push(0, srcIdx);

  while (heap.size > 0) {
    const { cost: d, node: u } = heap.pop();
    if (d > maxMin) break;
    if (d > dist[u]) continue;

    for (let e = ptr[u], end = ptr[u + 1]; e < end; e++) {
      const v  = edgeDst[e];
      const nd = d + costs[e];
      if (nd < dist[v]) {
        dist[v] = nd;
        heap.push(nd, v);
      }
    }
  }

  return dist;
}

// ─────────────────────────────────────────────
// 経路 Dijkstra（1対1・前ノード記録）
// ─────────────────────────────────────────────
function dijkstraRoute(srcIdx, dstIdx, mode) {
  const costs    = mode === 'vehicle' ? edgeCosV : edgeCosW;
  const dist     = new Float32Array(nNodes).fill(Infinity);
  const predNode = new Int32Array(nNodes).fill(-1);
  const predLink = new Uint32Array(nNodes);
  dist[srcIdx]   = 0;

  const heap = new MinHeap();
  heap.push(0, srcIdx);

  while (heap.size > 0) {
    const { cost: d, node: u } = heap.pop();
    if (u === dstIdx) break;
    if (d > dist[u]) continue;

    for (let e = ptr[u], end = ptr[u + 1]; e < end; e++) {
      const v  = edgeDst[e];
      const nd = d + costs[e];
      if (nd < dist[v]) {
        dist[v]     = nd;
        predNode[v] = u;
        predLink[v] = edgeLid[e];
        heap.push(nd, v);
      }
    }
  }

  if (!isFinite(dist[dstIdx])) return { link_ids: [], dist_min: null };

  const link_ids = [];
  let node = dstIdx;
  while (predNode[node] !== -1) {
    link_ids.push(predLink[node]);
    node = predNode[node];
  }
  link_ids.reverse();

  return { link_ids, dist_min: dist[dstIdx] };
}

// ─────────────────────────────────────────────
// 到達圏結果: リンク単位の最短時間（両端ノードの小さい方）
// ─────────────────────────────────────────────
function linkDistances(nodeDist, maxMin) {
  const result = {};
  for (let i = 0; i < nLinks; i++) {
    const d1 = nodeDist[node1[i]];
    const d2 = nodeDist[node2[i]];
    const d  = d1 < d2 ? d1 : d2;
    if (d <= maxMin) {
      result[linkId[i]] = Math.round(d * 10) / 10;
    }
  }
  return result;
}

// ─────────────────────────────────────────────
// メッセージハンドラ
// ─────────────────────────────────────────────
self.onmessage = async ({ data }) => {
  if (data.type === 'load') {
    try {
      const t0 = performance.now();
      await loadGraph(data.url);
      const ms = Math.round(performance.now() - t0);
      self.postMessage({ type: 'ready', nLinks, nNodes, loadMs: ms });
    } catch (e) {
      self.postMessage({ type: 'error', message: e.message });
    }
    return;
  }

  if (data.type === 'reachability') {
    const t0 = performance.now();
    const { idx: srcIdx, snapM } = nearestNode(data.lat, data.lon);
    const nodeDist = dijkstraReach(srcIdx, data.max_min, data.mode);
    const links    = linkDistances(nodeDist, data.max_min);
    const ms = Math.round(performance.now() - t0);

    self.postMessage({
      type: 'reachability_result',
      links,
      meta: {
        snap_m:          Math.round(snapM),
        reachable_links: Object.keys(links).length,
        calc_ms:         ms,
      },
    });
    return;
  }

  if (data.type === 'route') {
    const t0 = performance.now();
    const { idx: origIdx, snapM: origSnap } = nearestNode(data.orig_lat, data.orig_lon);
    const { idx: destIdx, snapM: destSnap } = nearestNode(data.dest_lat, data.dest_lon);
    const { link_ids, dist_min } = dijkstraRoute(origIdx, destIdx, data.mode);
    const ms = Math.round(performance.now() - t0);

    self.postMessage({
      type: 'route_result',
      link_ids,
      meta: {
        dist_min:    dist_min !== null ? Math.round(dist_min * 100) / 100 : null,
        link_count:  link_ids.length,
        orig_snap_m: Math.round(origSnap),
        dest_snap_m: Math.round(destSnap),
        calc_ms:     ms,
        error:       dist_min === null ? '到達不能' : null,
      },
    });
    return;
  }
};
