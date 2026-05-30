"""
経路探索・到達圏 Web API

起動:
  uvicorn src.main:app --host 0.0.0.0 --port 8080 --reload

環境変数:
  LINKS_PATH  道路リンク parquet のパス
  NODES_PATH  道路ノード parquet のパス
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .graph import RouterGraph

_ROOT = Path(__file__).parent.parent
_DEFAULT_LINKS = str(_ROOT / "network/saitama/KSJ_N13-24_saitama_all_道路リンク.parquet")
_DEFAULT_NODES = str(_ROOT / "network/saitama/KSJ_N13-24_saitama_all_道路ノード.parquet")

LINKS_PATH = os.environ.get("LINKS_PATH", _DEFAULT_LINKS)
NODES_PATH = os.environ.get("NODES_PATH", _DEFAULT_NODES)

_graph: RouterGraph | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _graph
    _graph = RouterGraph(LINKS_PATH, NODES_PATH)
    yield


app = FastAPI(
    title="Route Search API",
    description="KSJ 道路データによる経路探索・到達圏 API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# PMTiles を静的配信（/static/network/...）
_network_dir = _ROOT / "network"
_viewer_dir  = _ROOT / "viewer"
if _network_dir.exists():
    app.mount("/static/network", StaticFiles(directory=str(_network_dir)), name="network")

# ビューワーファイルを個別ルートで配信（/ への StaticFiles マウントは API と衝突するため）
@app.get("/", include_in_schema=False)
def viewer_index():
    return FileResponse(str(_viewer_dir / "index.html"))

@app.get("/pale.json", include_in_schema=False)
def viewer_pale():
    return FileResponse(str(_viewer_dir / "pale.json"))


# ─────────────────────────────────────────────
# リクエストモデル
# ─────────────────────────────────────────────
class ReachabilityRequest(BaseModel):
    lat:     float = Field(..., description="始点緯度")
    lon:     float = Field(..., description="始点経度")
    max_min: float = Field(60.0, ge=1, le=240, description="到達圏の上限時間（分）")
    mode:    str   = Field("vehicle", pattern="^(vehicle|walk)$")


class RouteRequest(BaseModel):
    orig_lat: float = Field(..., description="始点緯度")
    orig_lon: float = Field(..., description="始点経度")
    dest_lat: float = Field(..., description="終点緯度")
    dest_lon: float = Field(..., description="終点経度")
    mode:     str   = Field("vehicle", pattern="^(vehicle|walk)$")


# ─────────────────────────────────────────────
# エンドポイント
# ─────────────────────────────────────────────
@app.get("/healthz", tags=["system"])
def healthz():
    return {"status": "ok", "graph_loaded": _graph is not None}


@app.post("/reachability", tags=["routing"])
def reachability(req: ReachabilityRequest):
    """
    始点から max_min 分以内に到達できる道路リンクを返す。

    レスポンス:
    - `links`: `{link_id: dist_min}` — クライアントが PMTiles の setFeatureState に使用
    - `meta.snap_m`: 始点スナップ距離（m）
    - `meta.reachable_links`: 到達可能リンク数
    """
    if _graph is None:
        raise HTTPException(503, "グラフ未ロード")
    return _graph.reachability(req.lat, req.lon, req.max_min, req.mode)


@app.post("/route", tags=["routing"])
def route(req: RouteRequest):
    """
    始点から終点までの最短経路を link_id リストで返す。

    レスポンス:
    - `link_ids`: 経路を構成するリンクIDのリスト（始点→終点順）
    - `meta.dist_min`: 総所要時間（分）
    - `meta.link_count`: 経路リンク数
    """
    if _graph is None:
        raise HTTPException(503, "グラフ未ロード")
    return _graph.route(req.orig_lat, req.orig_lon,
                        req.dest_lat, req.dest_lon, req.mode)
