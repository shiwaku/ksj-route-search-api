# 公開版（Cloudflare Containers）の API イメージ。ビルドコンテキストはリポジトリ直下（wrangler.jsonc の image_build_context）。
# ルートの Dockerfile（compose 用）との違いは 2 点:
#   - グラフ parquet（380 MB）をイメージに焼き込む。compose は bind mount（deploy-cloudflare.md 4-2）
#   - DATABASE_URL を設定しない = DB なし。/bookmarks /bench は登録されない（同 5 章）
# ignore はルートの .dockerignore（許可リスト・parquet を含む）
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
# glibc はスレッドごとにメモリ領域（arena）を作り、解放しても手放さない。探索はスレッドプールで回るので絞る
ENV MALLOC_ARENA_MAX=2

# レイヤは変わりにくい順: 依存 → parquet → コード。コードだけ直したときは小さいレイヤしか上がらない
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY network/nationwide/*.parquet network/nationwide/
COPY api ./api

EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
