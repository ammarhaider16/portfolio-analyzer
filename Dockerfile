FROM ghcr.io/astral-sh/uv:0.13.1 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PORTFOLIO_DATA_DIR=/data PORTFOLIO_SERVER_MODE=1
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev && useradd --create-home --uid 10001 portfolio && mkdir /data && chown portfolio:portfolio /data
USER portfolio
EXPOSE 8000
CMD ["/app/.venv/bin/betafolio", "serve", "--server", "--host", "0.0.0.0"]
