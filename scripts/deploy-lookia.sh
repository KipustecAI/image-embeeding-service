#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 <dev|prod>" >&2
  exit 64
}

[[ $# -eq 1 ]] || usage
environment=$1

case "$environment" in
  dev)
    fleet_expected=36 lucam_expected=4
    expected_host=srv917124
    branch=dev
    project=lookia-dev
    microservices=/root/projects/microservices
    backup_parent=/root
    ready_target=lookia-ready-dev
    update_target=dev-update
    run_target=dev-run
    ;;
  prod)
    fleet_expected=36 lucam_expected=5
    expected_host=srv1880388
    branch=main
    project=lookia-prod
    microservices=/home/stanley/project/microservices
    backup_parent=/home/stanley
    ready_target=lookia-ready-prod
    update_target=prod-update
    run_target=prod-run
    ;;
  *) usage ;;
esac

service=ms-embedding-api
image=lucam/image-embeeding-api:latest
source_repo=$microservices/image-embeeding-service
orchestrator=$microservices/video-server_microservicios-orchestrated
runtime_env=$orchestrator/configs/lookia/$environment/image-embedding-service.env
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_root=$backup_parent/lookia-embedding-$environment-cicd-$stamp
rollback_tag=lucam/image-embeeding-api:rollback-$environment-$stamp
failed_tag=lucam/image-embeeding-api:failed-$environment-$stamp
rollback_armed=false
container_mutated=false

compose=(
  docker compose
  --env-file "$orchestrator/docker/lookia/compose.$environment.env"
  -f "$orchestrator/docker/lookia/docker-compose.yaml"
  --profile lookia-app
  --profile dw-workers
)

rollback() {
  rc=$?
  trap - ERR
  if [[ "$rollback_armed" == true ]]; then
    current_id=$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null || true)
    [[ -z "$current_id" ]] || docker image tag "$current_id" "$failed_tag" || true
    docker image tag "$rollback_tag" "$image" || true
    if [[ "$container_mutated" == true ]]; then
      make -C "$orchestrator" "$update_target" SERVICE="$service" || true
    fi
    echo "Deployment failed; previous image tag restored. Database dump: $backup_root/embedding-before.dump" >&2
    echo "Database state was not restored automatically." >&2
  fi
  exit "$rc"
}
trap rollback ERR

[[ "$(hostname -s)" == "$expected_host" ]]
timeout 10 docker info >/dev/null
[[ -d "$source_repo/.git" && -d "$orchestrator/.git" && -f "$runtime_env" ]]
[[ "$(git -C "$source_repo" branch --show-current)" == "$branch" ]]
[[ -z "$(git -C "$source_repo" status --porcelain)" ]]
git -C "$orchestrator" diff --quiet
git -C "$orchestrator" diff --cached --quiet

source_revision=$(git -C "$source_repo" rev-parse HEAD)
remote_revision=$(git -C "$source_repo" ls-remote origin "refs/heads/$branch" | awk '{print $1; exit}')
[[ "$source_revision" == "$remote_revision" ]]
[[ -z "${EXPECTED_SHA:-}" || "$source_revision" == "$EXPECTED_SHA" ]]

git -C "$orchestrator" pull --ff-only origin main

# Readiness must pass before backup/build so a configuration failure cannot
# retag the image or create source/image/container drift.
make -C "$orchestrator" "$ready_target"

total=$(docker ps -aq --filter label=com.docker.compose.project="$project" | wc -l | tr -d ' ')
running=$(docker ps -q --filter label=com.docker.compose.project="$project" | wc -l | tr -d ' ')
bad=$(docker ps -a --filter label=com.docker.compose.project="$project" --format '{{.Status}}' | grep -Eic 'unhealthy|restarting|exited|dead' || true)
lucam=$(docker ps -q --filter label=com.docker.compose.project=lucam-stack | wc -l | tr -d ' ')
# Counts only ACTIVE or restart-enabled legacy containers. A RETIRED record —
# exited with restart policy 'no' — is deliberately tolerated: it is a historical
# artifact, not a running duplicate, and `docker ps -aq | wc -l` cannot tell the
# difference. ms-sicoes-agent-service (retired 2026-09-08) had been failing this
# gate and blocking every release from this repository since 2026-09-05.
legacy=$(docker ps -aq --filter label=com.docker.compose.project=video-server_microservicios-orchestrated \
  | xargs -r docker inspect -f '{{.State.Status}} {{.HostConfig.RestartPolicy.Name}}' \
  | grep -vc '^exited no$' || true)
[[ "$running" == "$fleet_expected" && "$total" == "$fleet_expected" && "$bad" == 0 && "$lucam" == "$lucam_expected" && "$legacy" == 0 ]]

container_id=$(docker ps -q --filter label=com.docker.compose.project="$project" --filter label=com.docker.compose.service="$service" | head -n1)
[[ -n "$container_id" ]]
old_image_id=$(docker inspect --format '{{.Image}}' "$container_id")

umask 077
install -d -m 0700 "$backup_root"
git -C "$source_repo" status --short --branch > "$backup_root/source-status.txt"
git -C "$source_repo" log -n 30 --oneline --decorate > "$backup_root/source-log.txt"
git -C "$source_repo" bundle create "$backup_root/source.bundle" --all
git -C "$orchestrator" status --short --branch > "$backup_root/orchestrator-status.txt"
git -C "$orchestrator" log -n 30 --oneline --decorate > "$backup_root/orchestrator-log.txt"
git -C "$orchestrator" bundle create "$backup_root/orchestrator.bundle" --all
install -m 0600 "$runtime_env" "$backup_root/image-embedding-service.env"
docker inspect --format 'container={{.Name}} image={{.Config.Image}} image_id={{.Image}} state={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restarts={{.RestartCount}} started={{.State.StartedAt}}' "$container_id" > "$backup_root/container-before.txt"
timeout 20 docker logs --tail 500 "$container_id" > "$backup_root/container-before.log" 2>&1 || true
docker ps -a --filter label=com.docker.compose.project="$project" --format '{{.ID}} {{.Image}} {{.Names}} {{.Status}}' > "$backup_root/lookia-before.txt"
docker ps -a --filter label=com.docker.compose.project=lucam-stack --format '{{.ID}} {{.Image}} {{.Names}} {{.Status}}' > "$backup_root/lucam-before.txt"
docker image tag "$old_image_id" "$rollback_tag"

db_url=$(awk -F= '$1 == "DATABASE_URL" {print substr($0,index($0,"=")+1); exit}' "$runtime_env")
[[ -n "$db_url" ]]
db_url=${db_url/postgresql+asyncpg:\/\//postgresql:\/\/}
db_url=${db_url//ssl=require/sslmode=require}
db_url=${db_url//ssl=true/sslmode=require}
# pg_dump runs set_config('search_path', '', false). Through the Neon pooler (transaction mode)
# that setting stays on the shared server connection and later clients get an empty
# search_path ("no schema has been selected to create in"). Dump through the direct endpoint.
db_url=${db_url/-pooler./.}
docker image inspect postgres:18-alpine >/dev/null 2>&1 || docker pull postgres:18-alpine
docker run --rm -e DATABASE_URL="$db_url" postgres:18-alpine \
  sh -c 'pg_dump --format=custom --no-owner --no-acl "$DATABASE_URL"' \
  > "$backup_root/embedding-before.dump"
unset db_url
chmod 600 "$backup_root/embedding-before.dump"
sha256sum "$backup_root/embedding-before.dump" > "$backup_root/embedding-before.dump.sha256"

build_time=$(date -u +%Y-%m-%dT%H:%M:%SZ)
docker build \
  --build-arg "GIT_COMMIT_SHA=$source_revision" \
  --build-arg "BUILD_TIME=$build_time" \
  --label "org.opencontainers.image.revision=$source_revision" \
  --label "org.opencontainers.image.ref.name=$branch" \
  --label "org.opencontainers.image.created=$build_time" \
  -t "$image" "$source_repo"

rollback_armed=true
new_image_id=$(docker image inspect --format '{{.Id}}' "$image")
new_revision=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")
[[ "$new_revision" == "$source_revision" ]]

docker run --rm --entrypoint alembic "$image" heads > "$backup_root/alembic-heads.txt"
migration_head_count=$(awk 'NF {count++} END {print count+0}' "$backup_root/alembic-heads.txt")
migration_head=$(awk 'NF {print $1; exit}' "$backup_root/alembic-heads.txt")
[[ "$migration_head_count" == 1 && -n "$migration_head" ]]

make -C "$orchestrator" "$run_target" SERVICE="$service" \
  ARGS="alembic upgrade head" > "$backup_root/alembic-upgrade.log" 2>&1
make -C "$orchestrator" "$run_target" SERVICE="$service" \
  ARGS="alembic current" > "$backup_root/alembic-current.log" 2>&1
grep -Fq "$migration_head (head)" "$backup_root/alembic-current.log"

container_mutated=true
make -C "$orchestrator" "$update_target" SERVICE="$service"

container_id=$("${compose[@]}" ps -q "$service")
[[ -n "$container_id" ]]
container_image_id=$(docker inspect --format '{{.Image}}' "$container_id")
container_revision=$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$container_id")
state=$(docker inspect --format '{{.State.Status}}' "$container_id")
health=$(docker inspect --format '{{.State.Health.Status}}' "$container_id")
restarts=$(docker inspect --format '{{.RestartCount}}' "$container_id")
[[ "$container_image_id" == "$new_image_id" && "$container_revision" == "$source_revision" ]]
[[ "$state" == running && "$health" == healthy && "$restarts" == 0 ]]

docker exec -i "$container_id" python - <<'PY'
import json
import os
from urllib.request import urlopen

for path in ("/health",):  # image-embedding exposes only /health
    with urlopen(f"http://127.0.0.1:8001{path}", timeout=10) as response:
        assert response.status == 200, (path, response.status)
        payload = json.load(response)
        print(f"{path}=200")
        if path == "/ready":
            assert payload["status"] == "ready", payload
PY

logs=$(timeout 20 docker logs --since 10m "$container_id" 2>&1 || true)
startup=$(printf '%s\n' "$logs" | grep -c 'Application startup complete' || true)
fatal=$(printf '%s\n' "$logs" | grep -Eic 'Traceback|CRITICAL|Unhandled|Application startup failed|alembic.*ERROR' || true)
[[ "$startup" -ge 1 && "$fatal" == 0 ]]

total=$(docker ps -aq --filter label=com.docker.compose.project="$project" | wc -l | tr -d ' ')
running=$(docker ps -q --filter label=com.docker.compose.project="$project" | wc -l | tr -d ' ')
bad=$(docker ps -a --filter label=com.docker.compose.project="$project" --format '{{.Status}}' | grep -Eic 'unhealthy|restarting|exited|dead' || true)
lucam=$(docker ps -q --filter label=com.docker.compose.project=lucam-stack | wc -l | tr -d ' ')
# Counts only ACTIVE or restart-enabled legacy containers. A RETIRED record —
# exited with restart policy 'no' — is deliberately tolerated: it is a historical
# artifact, not a running duplicate, and `docker ps -aq | wc -l` cannot tell the
# difference. ms-sicoes-agent-service (retired 2026-09-08) had been failing this
# gate and blocking every release from this repository since 2026-09-05.
legacy=$(docker ps -aq --filter label=com.docker.compose.project=video-server_microservicios-orchestrated \
  | xargs -r docker inspect -f '{{.State.Status}} {{.HostConfig.RestartPolicy.Name}}' \
  | grep -vc '^exited no$' || true)
[[ "$running" == "$fleet_expected" && "$total" == "$fleet_expected" && "$bad" == 0 && "$lucam" == "$lucam_expected" && "$legacy" == 0 ]]

rollback_armed=false
trap - ERR
printf 'Image Embedding API %s deploy complete: revision=%s image=%s migration=%s backup=%s\n' \
  "$environment" "$source_revision" "$new_image_id" "$migration_head" "$backup_root"
