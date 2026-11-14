#!/bin/bash
# Одна команда: закоммитить подготовленные правки, отправить на GitHub и выложить сайт целиком.
#
#   ./release_all.sh                      # сообщение коммита по умолчанию
#   ./release_all.sh "Что изменилось"     # своё сообщение коммита
#
# Шаги: пароль сайта (спросит один раз и сохранит вне папки проекта) -> git commit -> git push
#       -> запуск Docker -> deploy_yc.sh -> проверка живого сайта.
# Любая ошибка останавливает скрипт; запускать повторно безопасно.
set -euo pipefail
cd "$(dirname "$0")"
URL="https://bba2agi0c621jbhicgod.containers.yandexcloud.net"
AUTH_FILE="$HOME/.config/macro/site_auth.env"

echo "=== 1/5 Пароль сайта"
if [ ! -f "$AUTH_FILE" ]; then
  echo "«Макропрогнозы» останутся открытыми, остальные вкладки будут по логину и паролю."
  echo "Придумайте их сейчас (без запятых и пробелов). Пароль при вводе не отображается."
  read -r -p "Логин: " U
  read -r -s -p "Пароль: " P; echo
  case "$U$P" in *,*|*" "*|"") echo "Пустое значение, пробел или запятая — запустите скрипт заново."; exit 1;; esac
  [ "$P" = "pass" ] && { echo "Пароль «pass» не подходит."; exit 1; }
  mkdir -p "$(dirname "$AUTH_FILE")"
  ( umask 077; printf 'BASIC_AUTH_USER=%s\nBASIC_AUTH_PASS=%s\n' "$U" "$P" > "$AUTH_FILE" )
  echo "Сохранено в $AUTH_FILE"
else
  echo "Файл с паролем уже есть: $AUTH_FILE"
fi
BASIC_AUTH_USER=""; BASIC_AUTH_PASS=""
. "$AUTH_FILE"

echo "=== 2/5 Коммит"
if git diff --cached --quiet; then
  echo "В очереди на коммит ничего нет — пропускаю."
else
  git commit -m "${1:-Обновление сайта}"
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "ВНИМАНИЕ: в папке остались незакоммиченные изменения (они тоже попадут в выкладку):"
  git status --short
fi

echo "=== 3/5 Отправка на GitHub"
git push origin main

echo "=== 4/5 Docker и выкладка (3-6 минут)"
if ! docker info >/dev/null 2>&1; then
  open -a Docker
  printf "Жду запуска Docker"
  for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; printf "."; sleep 3; done; echo
  docker info >/dev/null 2>&1 || { echo "Docker не запустился — откройте Docker Desktop и повторите."; exit 1; }
fi
./deploy_yc.sh

echo "=== 5/5 Проверка живого сайта"
sleep 20
code() { curl -s -o /dev/null -w "%{http_code}" -m 60 "$@" || echo "нет ответа"; }
FAIL=0
check() {  # check "описание" ожидаемый_код фактический_код
  if [ "$2" = "$3" ]; then echo "  ок      $1 -> $3"; else echo "  ОШИБКА  $1 -> $3 (ожидалось $2)"; FAIL=1; fi
}
JAR="$(mktemp)"; trap 'rm -f "$JAR"' EXIT
check "Макропрогнозы без входа" 200 "$(code "$URL/")"
check "вход с логином и паролем" 302 "$(code -c "$JAR" --data-urlencode "username=$BASIC_AUTH_USER" \
      --data-urlencode "password=$BASIC_AUTH_PASS" --data-urlencode "next=/" "$URL/login")"
for p in /ruble /ngd /ofz /yield-forecast /cbr-models /inflation/weekly /inflation/monthly; do
  check "$p без входа (уводит на форму)" 302 "$(code "$URL$p")"
  check "$p после входа" 200 "$(code -b "$JAR" "$URL$p")"
done
[ "$FAIL" = "0" ] && echo "Готово: сайт выложен, вход работает." || { echo "Есть ошибки — пришлите этот вывод Claude."; exit 1; }
