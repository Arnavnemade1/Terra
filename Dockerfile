FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/backend \
    TERRA_DATA_DIR=/data \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    GDAL_CACHEMAX=64 \
    TERRA_MAX_ACTIVITIES=3 \
    TERRA_MAX_WORKFLOW_TASKS=4 \
    TERRA_TILE_WARM=0

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libexpat1 libgomp1 tini \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY backend ./backend
RUN pip install --no-cache-dir . \
    && python -c "import rasterio, h5py, numpy, scipy, pyproj; import terra.science" \
    && mkdir -p /data

EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "terra.runtime"]
