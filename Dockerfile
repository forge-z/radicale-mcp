FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    MCP_TIMEZONE=America/Sao_Paulo
WORKDIR /app

COPY pyproject.toml README.md ./
COPY radicale_mcp/ ./radicale_mcp/
RUN python -m pip install --no-cache-dir . Radicale==3.8.0 "bcrypt>=4,<6" \
    && mkdir -p /var/lib/radicale \
    && chown 10001:10001 /var/lib/radicale

COPY docker/radicale/config /etc/radicale/config
RUN touch /etc/radicale/users
COPY docker/supervise.py /usr/local/bin/radicale-mcp-supervise
EXPOSE 5232 8080
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:' + __import__('os').environ.get('PORT', '8080') + '/health', timeout=2)"]
ENTRYPOINT ["python", "/usr/local/bin/radicale-mcp-supervise"]
