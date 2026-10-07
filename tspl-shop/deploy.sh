#!/usr/bin/env bash
# Deploys the TSPL Label Print shop (YooKassa payments + license keys) to Yandex Cloud Functions.
#
# First run (needs a person at the keyboard): creates the service account, the Lockbox
# secrets (YooKassa secret key and Yandex Mail app password are asked interactively,
# the license private key is read from a file — none of them is stored in this repo),
# and the public function. Then it writes the function URL into tspl/index.html and
# tspl/thanks.html.
# Next runs: only upload a new version of the code.
#
# Switching from the YooKassa test shop to the real one:
#   YOOKASSA_RESET=1 YOOKASSA_SHOP_ID=<real shop id> ./tspl-shop/deploy.sh
#
# Requires: yc CLI configured with `yc init`, python3, zip.
# Settings (environment variables, all optional):
#   YOOKASSA_SHOP_ID  asked on the first run, then remembered in the function
#   PRICE_RUB         default 499
#   RECEIPTS          1 = send 54-FZ receipts through YooKassa (default 0)
#   PRIVATE_KEY_FILE  default ~/src/TSPLLabelPrint/Licensing/private_key.txt

set -euo pipefail
cd "$(dirname "$0")"

FUNCTION_NAME="tspl-shop"
SA_NAME="tspl-shop-sa"
SECRET_NAME="tspl-shop-secrets"
SMTP_SECRET_NAME="impression-form-smtp"      # shared with form-handler/
SMTP_USER="${SMTP_USER:-chardymov@yandex.ru}"
MAIL_TO="${MAIL_TO:-chardymov@yandex.ru}"
PRICE_RUB="${PRICE_RUB:-499}"
RECEIPTS="${RECEIPTS:-0}"
PRIVATE_KEY_FILE="${PRIVATE_KEY_FILE:-$HOME/src/TSPLLabelPrint/Licensing/private_key.txt}"
SITE_DIR="$(cd .. && pwd)"

json_field() { python3 -c 'import json,sys; d=json.load(sys.stdin)
for k in sys.argv[1].split("."): d=d[k]
print(d)' "$1"; }

# yc may not be on PATH yet (fresh install, or a shell that didn't read ~/.zshrc).
YC_BIN=$(command -v yc || echo "$HOME/yandex-cloud/bin/yc")
[ -x "$YC_BIN" ] || { echo "Не найден yc. Установка: curl -sSL https://storage.yandexcloud.net/yandexcloud-yc/install.sh | bash"; exit 1; }
# Recent yc versions need the API endpoint set explicitly for serverless commands.
yc() { "$YC_BIN" --endpoint "${YC_ENDPOINT:-api.cloud.yandex.net:443}" "$@"; }

FOLDER_ID=$(yc config get folder-id 2>/dev/null || true)
[ -n "$FOLDER_ID" ] || { echo "yc ещё не настроен. Запустите в Терминале: $YC_BIN init"; exit 1; }
echo "Folder: $FOLDER_ID"

# Secrets are typed in by a person; refuse to run where nobody can type.
need_tty() { [ -t 0 ] || { echo "Нужен ввод с клавиатуры — запустите скрипт в окне Терминала."; exit 1; }; }

# 0. Shop id: from the environment, from the deployed function, or asked.
if [ -z "${YOOKASSA_SHOP_ID:-}" ]; then
  YOOKASSA_SHOP_ID=$(yc serverless function version get-by-tag --function-name "$FUNCTION_NAME" \
    --tag '$latest' --format json 2>/dev/null | python3 -c 'import json,sys
try: print(json.load(sys.stdin)["environment"]["YOOKASSA_SHOP_ID"])
except Exception: pass' || true)
fi
if [ -z "${YOOKASSA_SHOP_ID:-}" ]; then
  need_tty
  read -r -p "shopId магазина ЮKassa (Интеграция → Ключи API): " YOOKASSA_SHOP_ID
fi
[ -n "$YOOKASSA_SHOP_ID" ] || { echo "Нужен shopId."; exit 1; }

# 1. Service account the function runs as (it may only read its secrets).
if ! yc iam service-account get --name "$SA_NAME" >/dev/null 2>&1; then
  echo "Creating service account ${SA_NAME}…"
  yc iam service-account create --name "$SA_NAME" >/dev/null
fi
SA_ID=$(yc iam service-account get --name "$SA_NAME" --format json | json_field id)

# 2a. Shop secrets: YooKassa secret key + license private key.
make_shop_payload() {
  [ -f "$PRIVATE_KEY_FILE" ] || { echo "Не найден $PRIVATE_KEY_FILE"; exit 1; }
  echo
  echo "Секретный ключ ЮKassa для магазина $YOOKASSA_SHOP_ID"
  echo "(ЮKassa → Интеграция → Ключи API; тестовый начинается с test_, боевой с live_)."
  need_tty
  read -r -s -p "Секретный ключ (ввод не отображается): " YK_SECRET; echo
  [ -n "$YK_SECRET" ] || { echo "Пустой ключ, отмена."; exit 1; }
  SHOP_PAYLOAD=$(YK_SECRET="$YK_SECRET" KEY_FILE="$PRIVATE_KEY_FILE" python3 -c 'import json,os
print(json.dumps([
  {"key": "YOOKASSA_SECRET_KEY", "text_value": os.environ["YK_SECRET"]},
  {"key": "TSPL_PRIVATE_KEY", "text_value": open(os.environ["KEY_FILE"]).read().strip()},
]))')
  unset YK_SECRET
}
if ! yc lockbox secret get --name "$SECRET_NAME" >/dev/null 2>&1; then
  make_shop_payload
  yc lockbox secret create --name "$SECRET_NAME" --payload "$SHOP_PAYLOAD" >/dev/null
  unset SHOP_PAYLOAD
  echo "Secret $SECRET_NAME created."
elif [ "${YOOKASSA_RESET:-0}" = "1" ]; then
  make_shop_payload
  yc lockbox secret add-version --name "$SECRET_NAME" --payload "$SHOP_PAYLOAD" >/dev/null
  unset SHOP_PAYLOAD
  echo "Secret $SECRET_NAME updated."
fi

# 2b. SMTP password (the same secret the request form uses).
if ! yc lockbox secret get --name "$SMTP_SECRET_NAME" >/dev/null 2>&1; then
  echo
  echo "Нужен пароль приложения Яндекс Почты для $SMTP_USER"
  echo "(id.yandex.ru → Безопасность → Пароли приложений → Почта)."
  need_tty
  read -r -s -p "Пароль приложения (ввод не отображается): " SMTP_PASSWORD; echo
  [ -n "$SMTP_PASSWORD" ] || { echo "Пустой пароль, отмена."; exit 1; }
  PAYLOAD=$(SMTP_PASSWORD="$SMTP_PASSWORD" python3 -c 'import json,os
print(json.dumps([{"key": "SMTP_PASSWORD", "text_value": os.environ["SMTP_PASSWORD"]}]))')
  unset SMTP_PASSWORD
  yc lockbox secret create --name "$SMTP_SECRET_NAME" --payload "$PAYLOAD" >/dev/null
  unset PAYLOAD
  echo "Secret $SMTP_SECRET_NAME created."
fi

secret_ref() {  # name -> "id version"
  local js; js=$(yc lockbox secret get --name "$1" --format json)
  echo "$(echo "$js" | json_field id) $(echo "$js" | json_field current_version.id)"
}
read -r SHOP_SECRET_ID SHOP_SECRET_VER <<<"$(secret_ref "$SECRET_NAME")"
read -r SMTP_SECRET_ID SMTP_SECRET_VER <<<"$(secret_ref "$SMTP_SECRET_NAME")"
for id in "$SHOP_SECRET_ID" "$SMTP_SECRET_ID"; do
  yc lockbox secret add-access-binding --id "$id" \
    --role lockbox.payloadViewer --service-account-id "$SA_ID" >/dev/null 2>&1 || true
done

# 3. The function itself, public so the website and YooKassa can call it.
if ! yc serverless function get --name "$FUNCTION_NAME" >/dev/null 2>&1; then
  echo "Creating function ${FUNCTION_NAME}…"
  yc serverless function create --name "$FUNCTION_NAME" \
    --description "TSPL Label Print shop: YooKassa payments and license keys" >/dev/null
  yc serverless function allow-unauthenticated-invoke --name "$FUNCTION_NAME" >/dev/null
fi

# 4. Upload the code.
BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$BUILD_DIR"' EXIT
(cd src && zip -q "$BUILD_DIR/function.zip" index.py ed25519.py)

echo "Deploying new version…"
yc serverless function version create \
  --function-name "$FUNCTION_NAME" \
  --runtime python312 \
  --entrypoint index.handler \
  --memory 128m \
  --execution-timeout 20s \
  --source-path "$BUILD_DIR/function.zip" \
  --service-account-id "$SA_ID" \
  --environment "YOOKASSA_SHOP_ID=$YOOKASSA_SHOP_ID,SMTP_USER=$SMTP_USER,MAIL_TO=$MAIL_TO,PRICE_RUB=$PRICE_RUB,RECEIPTS=$RECEIPTS" \
  --secret "environment-variable=YOOKASSA_SECRET_KEY,id=$SHOP_SECRET_ID,version-id=$SHOP_SECRET_VER,key=YOOKASSA_SECRET_KEY" \
  --secret "environment-variable=TSPL_PRIVATE_KEY,id=$SHOP_SECRET_ID,version-id=$SHOP_SECRET_VER,key=TSPL_PRIVATE_KEY" \
  --secret "environment-variable=SMTP_PASSWORD,id=$SMTP_SECRET_ID,version-id=$SMTP_SECRET_VER,key=SMTP_PASSWORD" \
  >/dev/null

FUNCTION_ID=$(yc serverless function get --name "$FUNCTION_NAME" --format json | json_field id)
URL="https://functions.yandexcloud.net/$FUNCTION_ID"

# 5. Point the shop pages at the function.
for page in "$SITE_DIR/tspl/index.html" "$SITE_DIR/tspl/thanks.html"; do
  sed -i '' "s#__TSPL_SHOP_ENDPOINT__#$URL#g" "$page"
done

echo
echo "Готово. Адрес функции:"
echo "$URL"
echo
echo "Укажите его в ЮKassa: Интеграция → HTTP-уведомления → URL для уведомлений,"
echo "событие payment.succeeded. Затем закоммитьте tspl/index.html и tspl/thanks.html."
