# API（FastAPI）のイメージ。docker compose の api サービスから build される。
# 探索グラフの parquet（network/・290 MB）はイメージに焼かず、compose の bind mount で /app/network に渡す。
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
# glibc はスレッドごとにメモリ領域（arena）を作り、解放しても手放さない。探索はスレッドプールで回るので絞る
ENV MALLOC_ARENA_MAX=2

# 依存だけ先に入れてレイヤをキャッシュ（api/ を変えても再インストールしない）
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY api ./api

EXPOSE 8000
# api/graph.py の BASE は api/ の親＝/app。network/nationwide/*.parquet を /app/network に置く（mount）
CMD ["uv", "run", "--no-sync", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
