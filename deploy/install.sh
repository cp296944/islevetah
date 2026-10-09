#!/bin/sh
# Run as root on the NAS. This project only touches /volume3/islevet.
set -eu
PROJECT=/volume3/islevet
REPOSITORY=cp296944/islevetah
if [ "$(id -u)" != 0 ]; then echo "Run sudo -i before this script."; exit 1; fi
if [ ! -d "$PROJECT" ] || [ "$(readlink -f "$PROJECT")" != "$PROJECT" ]; then echo "Expected real project directory /volume3/islevet."; exit 1; fi
command -v docker >/dev/null
command -v curl >/dev/null
if docker compose version >/dev/null 2>&1; then COMPOSE='docker compose'; elif command -v docker-compose >/dev/null 2>&1; then COMPOSE='docker-compose'; else echo "Install Synology Container Manager first."; exit 1; fi
if docker inspect islevetah-app >/dev/null 2>&1; then
  SOURCE=$(docker inspect islevetah-app --format '{{index .Config.Labels "org.opencontainers.image.source"}}')
  if [ "$SOURCE" != "https://github.com/cp296944/islevetah" ]; then echo "Unexpected existing app container; stopped without changes."; exit 1; fi
else
  if netstat -lnt 2>/dev/null | awk '{print $4}' | grep -Eq '[:.]7788$'; then echo "Port 7788 is already in use."; exit 1; fi
fi
cd "$PROJECT"
docker pull python:3.12-slim
REVISION=$(curl -fsSL --retry 3 "https://api.github.com/repos/$REPOSITORY/commits/main" | docker run --rm -i python:3.12-slim python -c "import json,sys,re; s=json.load(sys.stdin)['sha']; assert re.fullmatch('[a-f0-9]{40}',s); print(s)")
STAGING=$(mktemp -d /tmp/islevetah-install.XXXXXXXX)
curl -fL --retry 3 "https://api.github.com/repos/$REPOSITORY/tarball/$REVISION" -o "$STAGING/source.tar.gz"
mkdir "$STAGING/source"
tar -xzf "$STAGING/source.tar.gz" --strip-components=1 -C "$STAGING/source"
# Source snapshot excludes live credentials and data. Data is backed up separately.
STAMP=$(date +%Y%m%d_%H%M%S)
mkdir -p "deployment-backups/$STAMP"
tar --exclude='./data' --exclude='./ota-state' --exclude='./.env' --exclude='./deployment-backups' -czf "deployment-backups/$STAMP/source.tar.gz" .
cp -R "$STAGING/source/." "$PROJECT/"
docker run --rm -v "$PROJECT:/workspace" -w /workspace python:3.12-slim python deploy/init_env.py
mkdir -p data ota-state
chown 10001:10001 data
chmod 700 data ota-state
export APP_VERSION="$REVISION"
$COMPOSE -p islevetah -f docker-compose.yml config --quiet
$COMPOSE -p islevetah -f docker-compose.yml build
if [ -f data/inventory.db ]; then
  $COMPOSE -p islevetah -f docker-compose.yml stop app
  docker run --rm -v "$PROJECT/data:/data" --entrypoint python islevetah-app:local -c "import sqlite3,time,os; from pathlib import Path; p=Path('/data/backups'); p.mkdir(exist_ok=True); s=sqlite3.connect('/data/inventory.db'); path=p/('deploy_'+str(int(time.time()))+'.db'); d=sqlite3.connect(path); s.backup(d);d.close();s.close();os.chmod(path,0o600)"
fi
$COMPOSE -p islevetah -f docker-compose.yml up -d --no-build
ATTEMPT=0
while [ "$ATTEMPT" -lt 60 ]; do
  if [ "$(docker inspect islevetah-app --format '{{.State.Health.Status}}')" = healthy ]; then
    echo "Installed revision $REVISION"
    echo "Website: http://192.168.0.2:7788"
    echo "First install: docker exec -it islevetah-app python server.py --init-admin admin"
    echo "Keep /volume3/islevet/data and .env for future updates."
    exit 0
  fi
  ATTEMPT=$((ATTEMPT + 1))
  sleep 2
done
echo "Health check failed. Check docker logs islevetah-app; backup retained."
exit 1
