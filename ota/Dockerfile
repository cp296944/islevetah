FROM python:3.12-slim
RUN pip install --no-cache-dir docker==7.1.0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY updater.py ./
EXPOSE 7790
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7790/health',timeout=3)"
CMD ["python", "updater.py"]
