FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HOST=0.0.0.0 PORT=8000 ID3_WEB_DATA=/app/web-data
RUN --mount=type=secret,id=build_ca,target=/etc/ssl/certs/ca-certificates.crt,mode=0444,required=false sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources && apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md requirements-runtime.lock ./
COPY music_manager ./music_manager
RUN --mount=type=secret,id=build_ca,target=/etc/ssl/certs/ca-certificates.crt,mode=0444,required=false pip install -r requirements-runtime.lock && pip install --no-deps --no-build-isolation . && chmod -R a+rX /app/music_manager && useradd --create-home --uid 10001 app && mkdir /app/web-data && chown app:app /app/web-data
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ['PORT']+'/healthz', timeout=3)"
CMD ["python", "-m", "music_manager.web"]
