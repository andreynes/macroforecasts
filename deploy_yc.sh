#!/usr/bin/env bash
set -euo pipefail

echo "=== Проверка инструментов ==="
command -v yc >/dev/null || { echo "yc не найден. Установи CLI Яндекс Облака и выполни 'yc init'."; exit 1; }
command -v docker >/dev/null || { echo "docker не найден. Установи Docker Desktop."; exit 1; }
python3 --version >/dev/null || { echo "Нужен python3 для парсинга JSON."; exit 1; }

echo "=== Получаю FOLDER_ID из профиля yc ==="
FOLDER_ID="$(yc config get folder-id || true)"
if [[ -z "${FOLDER_ID}" ]]; then
  echo "В профиле не задан folder-id. Покажу папки и предложу выбрать…"
  yc resource-manager folder list
  read -rp "Введи FOLDER_ID: " FOLDER_ID
  yc config set folder-id "${FOLDER_ID}"
fi
echo "FOLDER_ID=${FOLDER_ID}"

echo "=== Проверяю/создаю реестр образов (Container Registry) ==="
REGISTRY_ID="$(yc container registry list --folder-id "${FOLDER_ID}" --format json 2>/dev/null \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(next((x["id"] for x in d if x.get("status")=="ACTIVE"), ""), end="") if d else print("", end="")' || true)"

if [[ -z "${REGISTRY_ID}" ]]; then
  echo "Реестр не найден — создаю…"
  REG_JSON="$(yc container registry create --name macro-registry --folder-id "${FOLDER_ID}" --format json)"
  REGISTRY_ID="$(python3 - <<'PY'
import sys, json
print(json.load(sys.stdin)["id"])
PY
<<<"$REG_JSON")"
fi
echo "REGISTRY_ID=${REGISTRY_ID}"

echo "=== Проверяю/создаю сервисный аккаунт для рантайма контейнера ==="
SA_ID="$(yc iam service-account list --folder-id "${FOLDER_ID}" --format json \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(next((x["id"] for x in d if x.get("name")=="macro-sa"), ""), end="")' || true)"
if [[ -z "${SA_ID}" ]]; then
  SA_JSON="$(yc iam service-account create --name macro-sa --folder-id "${FOLDER_ID}" --format json)"
  SA_ID="$(python3 - <<'PY'
import sys, json
print(json.load(sys.stdin)["id"])
PY
<<<"$SA_JSON")"
fi
echo "SERVICE_ACCOUNT_ID=${SA_ID}"

echo "=== Выдаю роли (pull образа сервисному аккаунту) ==="
yc container registry add-access-binding --id "${REGISTRY_ID}" \
  --role container-registry.images.puller \
  --subject serviceAccount:"${SA_ID}" >/dev/null || true

echo "=== Логин Docker в реестр Яндекса ==="
yc container registry configure-docker >/dev/null

echo "=== Сборка и push образа ==="
CONT_NAME="macro-forecast-site"
IMAGE="cr.yandex/${REGISTRY_ID}/${CONT_NAME}:latest"
echo "IMAGE=${IMAGE}"

docker build --platform linux/amd64 -t "${IMAGE}" .
docker push "${IMAGE}"

echo "=== Создаю контейнер (если ещё не создан) ==="
yc serverless container create \
  --name "${CONT_NAME}" \
  --folder-id "${FOLDER_ID}" >/dev/null || true

echo "=== Деплой ревизии ==="
# ВАЖНО: без PORT — YC сам передаст PORT внутрь контейнера.
yc serverless container revision deploy \
  --container-name "${CONT_NAME}" \
  --image "${IMAGE}" \
  --service-account-id "${SA_ID}" \
  --cores 1 \
  --memory 512M \
  --execution-timeout 30s \
  --concurrency 1 \
  --environment DATA_FILE=/app/macro_main_linked.xlsx \
  --environment REQUIRE_BASIC_AUTH=0

echo "=== Делаю доступ по ссылке (allUsers -> invoker) ==="
yc serverless container add-access-binding \
  --name "${CONT_NAME}" \
  --role serverless.containers.invoker \
  --subject system:allUsers >/dev/null || true

echo "=== Готово. URL сервиса: ==="
yc serverless container get --name "${CONT_NAME}" --format json \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["url"])'
