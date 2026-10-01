#!/usr/bin/env bash
# Deploys the request-form handler to Yandex Cloud Functions.
#
# First run (needs a person at the keyboard): creates the service account,
# the Lockbox secret with the Yandex Mail app password (asked interactively,
# never stored in files), and the public function.
# Next runs: only upload a new version of the code.
#
# Requires: yc CLI, already configured with `yc init` (cloud + folder chosen).

set -euo pipefail
cd "$(dirname "$0")"

FUNCTION_NAME="impression-form"
SA_NAME="impression-form-sa"
SECRET_NAME="impression-form-smtp"
SMTP_USER="${SMTP_USER:-chardymov@yandex.ru}"
MAIL_TO="${MAIL_TO:-chardymov@yandex.ru}"

json_field() { python3 -c 'import json,sys; d=json.load(sys.stdin)
for k in sys.argv[1].split("."): d=d[k]
print(d)' "$1"; }

echo "Folder: $(yc config get folder-id)"

# 1. Service account the function runs as (it may only read the secret).
if ! yc iam service-account get --name "$SA_NAME" >/dev/null 2>&1; then
  echo "Creating service account $SA_NAME…"
  yc iam service-account create --name "$SA_NAME" >/dev/null
fi
SA_ID=$(yc iam service-account get --name "$SA_NAME" --format json | json_field id)

# 2. Lockbox secret with the SMTP app password.
if ! yc lockbox secret get --name "$SECRET_NAME" >/dev/null 2>&1; then
  echo
  echo "Нужен пароль приложения Яндекс Почты для $SMTP_USER"
  echo "(id.yandex.ru → Безопасность → Пароли приложений → Почта)."
  read -r -s -p "Пароль приложения (ввод не отображается): " SMTP_PASSWORD; echo
  [ -n "$SMTP_PASSWORD" ] || { echo "Пустой пароль, отмена."; exit 1; }
  PAYLOAD=$(SMTP_PASSWORD="$SMTP_PASSWORD" python3 -c 'import json,os
print(json.dumps([{"key": "SMTP_PASSWORD", "text_value": os.environ["SMTP_PASSWORD"]}]))')
  unset SMTP_PASSWORD
  yc lockbox secret create --name "$SECRET_NAME" --payload "$PAYLOAD" >/dev/null
  unset PAYLOAD
  echo "Secret $SECRET_NAME created."
fi
SECRET_JSON=$(yc lockbox secret get --name "$SECRET_NAME" --format json)
SECRET_ID=$(echo "$SECRET_JSON" | json_field id)
SECRET_VERSION=$(echo "$SECRET_JSON" | json_field current_version.id)
yc lockbox secret add-access-binding --id "$SECRET_ID" \
  --role lockbox.payloadViewer --service-account-id "$SA_ID" >/dev/null 2>&1 || true

# 3. The function itself, public so the website can call it.
if ! yc serverless function get --name "$FUNCTION_NAME" >/dev/null 2>&1; then
  echo "Creating function $FUNCTION_NAME…"
  yc serverless function create --name "$FUNCTION_NAME" \
    --description "Request form from impression-team.github.io/services.html" >/dev/null
  yc serverless function allow-unauthenticated-invoke --name "$FUNCTION_NAME" >/dev/null
fi

# 4. Upload the code.
BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$BUILD_DIR"' EXIT
(cd src && zip -q -r "$BUILD_DIR/function.zip" index.py)

echo "Deploying new version…"
yc serverless function version create \
  --function-name "$FUNCTION_NAME" \
  --runtime python312 \
  --entrypoint index.handler \
  --memory 128m \
  --execution-timeout 15s \
  --source-path "$BUILD_DIR/function.zip" \
  --service-account-id "$SA_ID" \
  --environment "SMTP_USER=$SMTP_USER,MAIL_TO=$MAIL_TO" \
  --secret "environment-variable=SMTP_PASSWORD,id=$SECRET_ID,version-id=$SECRET_VERSION,key=SMTP_PASSWORD" \
  >/dev/null

FUNCTION_ID=$(yc serverless function get --name "$FUNCTION_NAME" --format json | json_field id)
echo
echo "Готово. Адрес функции:"
echo "https://functions.yandexcloud.net/$FUNCTION_ID"
