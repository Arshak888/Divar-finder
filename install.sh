#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="https://github.com/Arshak888/Divar-finder.git"
INSTALL_DIR="/opt/Divar-finder"
PANEL_PORT="9000"

if [[ "${EUID}" -ne 0 ]]; then
  echo "برای نصب پیش‌نیازها دسترسی sudo لازم است."
  exec sudo -E bash "$0" "$@"
fi

if [[ ! -r /etc/os-release ]]; then
  echo "سیستم‌عامل پشتیبانی‌شده شناسایی نشد. این نصب‌کننده برای Ubuntu/Debian است."
  exit 1
fi
. /etc/os-release
if [[ "${ID:-}" != "ubuntu" && "${ID:-}" != "debian" && " ${ID_LIKE:-} " != *" ubuntu "* && " ${ID_LIKE:-} " != *" debian "* ]]; then
  echo "این نصب‌کننده فقط برای Ubuntu/Debian طراحی شده است."
  exit 1
fi

echo "==> نصب پیش‌نیازهای پایه"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git python3

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "==> نصب Docker Engine و Docker Compose از نصب‌کننده رسمی Docker"
  curl -fsSL https://get.docker.com -o /tmp/divar-finder-get-docker.sh
  sh /tmp/divar-finder-get-docker.sh
  rm -f /tmp/divar-finder-get-docker.sh
fi

systemctl enable --now docker

echo "==> دریافت پروژه"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  git -C "$INSTALL_DIR" pull --ff-only origin main
else
  if [[ -e "$INSTALL_DIR" ]]; then
    echo "مسیر $INSTALL_DIR وجود دارد ولی مخزن Git نیست؛ برای جلوگیری از حذف فایل‌ها توقف شد."
    exit 1
  fi
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"
if [[ ! -f .env ]]; then
  cp .env.example .env
fi

echo
echo "برای پنل وب یک رمز قوی وارد کن. رمز در فایل .env ذخیره می‌شود و نمایش داده نخواهد شد."
python3 - <<'PY'
from getpass import getpass
from pathlib import Path
import secrets
import sys

path = Path(".env")
text = path.read_text()
while True:
    password = getpass("رمز ورود پنل: ")
    if len(password) < 10:
        print("رمز باید حداقل ۱۰ کاراکتر باشد.")
        continue
    confirm = getpass("تکرار رمز ورود پنل: ")
    if password != confirm:
        print("رمزها مطابقت ندارند. دوباره تلاش کن.")
        continue
    break

def set_env(contents, key, value):
    lines = contents.splitlines()
    prefix = key + "="
    found = False
    out = []
    for line in lines:
        if line.startswith(prefix):
            if not found:
                out.append(prefix + value)
                found = True
        else:
            out.append(line)
    if not found:
        out.append(prefix + value)
    return "\n".join(out) + "\n"

text = set_env(text, "ADMIN_PASSWORD", password)
if "SECRET_KEY=replace_with_a_long_random_secret" in text or not any(x.startswith("SECRET_KEY=") and x.split("=",1)[1].strip() for x in text.splitlines()):
    text = set_env(text, "SECRET_KEY", secrets.token_urlsafe(48))
path.write_text(text)
path.chmod(0o600)
print("رمز پنل با موفقیت در .env تنظیم شد.")
PY

echo "==> ساخت و اجرای ربات و پنل"
docker compose up -d --build

SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "======================================================"
echo "نصب پایان یافت."
echo "پنل: http://${SERVER_IP:-IP-SERVER}:${PANEL_PORT}"
echo "برای امنیت، رمز پنل را در جای امن نگه دار."
echo "توکن ربات و آیدی عددی ادمین‌ها را از کارت «تنظیمات ربات تلگرام» در پنل وارد کن."
echo "اگر پنل از بیرون باز نمی‌شود، پورت TCP 9000 را در فایروال سرور/ارائه‌دهنده باز کن."
echo "لاگ‌ها: cd $INSTALL_DIR && docker compose logs --tail=100"
echo "======================================================"
