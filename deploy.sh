#!/usr/bin/env sh
set -eu

if [ ! -f .env ]; then
    cp .env.server.example .env
    echo "Created .env from .env.server.example."
    echo "Set a strong SECRET_KEY and any DashScope or Aliyun SMS credentials before starting."
    exit 1
fi

mkdir -p runtime/storage runtime/uploads runtime/exports
docker compose up -d --build
docker compose ps
curl --fail --silent --show-error http://127.0.0.1/api/health
