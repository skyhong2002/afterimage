# afterimage web service: the Astro frontend is built once, then served by the FastAPI app.
# Data (database, originals, thumbnails) lives in the /data volume, never in the image.
FROM node:24-alpine AS web
WORKDIR /app
COPY site/package.json site/package-lock.json ./
RUN npm ci
COPY site/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv
COPY service/requirements.txt ./
RUN pip install -r requirements.txt
COPY service/afterimage ./afterimage
COPY --from=web /app/dist ./site
ENV AFTERIMAGE_FRONTEND=/srv/site AFTERIMAGE_DATA=/data
USER 1000:1000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "afterimage.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
