FROM python:3.12-slim
WORKDIR /app
# git-lfs lets the runtime restore the progress DB if the platform's clone
# left LFS pointer files in the working tree (see start.sh bootstrap).
RUN apt-get update && apt-get install -y --no-install-recommends git git-lfs \
    && rm -rf /var/lib/apt/lists/* \
    && git lfs install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DB_PATH=/data/archive.db \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
CMD ["bash", "start.sh"]
