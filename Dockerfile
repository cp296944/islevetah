FROM python:3.12-slim
ARG APP_VERSION=development
LABEL org.opencontainers.image.source="https://github.com/cp296944/islevetah"
LABEL org.opencontainers.image.revision=$APP_VERSION
ENV APP_VERSION=$APP_VERSION PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 INVENTORY_DB=/data/inventory.db
WORKDIR /app
COPY server.py ./
COPY public ./public
# NAS source files may arrive with owner-only modes. Normalize image permissions
# before dropping privileges; runtime data keeps its separate private permissions.
RUN chmod 755 /app \
    && chmod 644 /app/server.py \
    && find /app/public -type d -exec chmod 755 {} + \
    && find /app/public -type f -exec chmod 644 {} + \
    && mkdir /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 7788
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=6 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7788/api/health',timeout=3)"
CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "7788"]
