FROM python:3.12-slim
# git + ca-certs so the service can `git pull` private progress updates
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY app.py .
ENV PORT=10000 REFRESH=30
EXPOSE 10000
CMD ["python3", "app.py"]
