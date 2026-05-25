#!/usr/bin/env bash
# Run on the site VPS as root (Ubuntu/Debian).
# Usage: bash install-on-server.sh [domain] [archive.tar.gz]
set -euo pipefail

DOMAIN="${1:-exsender.top}"
INSTALL_ROOT="/opt/exsender"
WEB_DIR="${INSTALL_ROOT}/web"
DATA_DIR="${INSTALL_ROOT}/data"
SERVICE_USER="exsender"
ARCHIVE="${2:-/tmp/exsender-site.tar.gz}"

PERSIST_FILES=(
  users.json
  bots.json
  invoices.json
  promos.json
  notifications.json
  admin_audit.json
  security_state.json
)

echo "==> exsender site install (${DOMAIN})"

if [[ ! -f "${ARCHIVE}" ]]; then
  echo "Archive not found: ${ARCHIVE}"
  echo "Upload exsender-site.tar.gz to /tmp first (deploy-site.ps1)."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx certbot python3-certbot-nginx curl ufw

ufw allow OpenSSH 2>/dev/null || true
ufw allow 80/tcp 2>/dev/null || true
ufw allow 443/tcp 2>/dev/null || true
ufw --force enable 2>/dev/null || true

if ! id "${SERVICE_USER}" &>/dev/null; then
  useradd --system --home "${INSTALL_ROOT}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

mkdir -p "${INSTALL_ROOT}" "${DATA_DIR}"

# Migrate legacy JSON from old web/ → data/ (one-time)
if [[ -d "${WEB_DIR}" ]]; then
  for f in "${PERSIST_FILES[@]}"; do
    if [[ -f "${WEB_DIR}/${f}" && ! -f "${DATA_DIR}/${f}" ]]; then
      echo "==> migrate ${f} -> ${DATA_DIR}/"
      cp -a "${WEB_DIR}/${f}" "${DATA_DIR}/${f}"
    fi
  done
  if [[ -f "${WEB_DIR}/.env" ]]; then
    cp -a "${WEB_DIR}/.env" "/tmp/exsender-env-backup.$$"
  fi
  if [[ -f "${WEB_DIR}/deploy_key" ]]; then
    cp -a "${WEB_DIR}/deploy_key" "/tmp/exsender-deploy-key-backup.$$" 2>/dev/null || true
    cp -a "${WEB_DIR}/deploy_key.pub" "/tmp/exsender-deploy-key-pub-backup.$$" 2>/dev/null || true
  fi
fi

# Code deploy — only replace web + frontend, never touch data/
rm -rf "${INSTALL_ROOT}/web" "${INSTALL_ROOT}/frontend"
tar -xzf "${ARCHIVE}" -C "${INSTALL_ROOT}"

# Restore .env (keep secrets across redeploys)
if [[ -f "/tmp/exsender-env-backup.$$" ]]; then
  cp -a "/tmp/exsender-env-backup.$$" "${WEB_DIR}/.env"
  rm -f "/tmp/exsender-env-backup.$$"
fi

if [[ ! -f "${WEB_DIR}/.env" ]]; then
  if [[ -f "${WEB_DIR}/.env.example" ]]; then
    cp "${WEB_DIR}/.env.example" "${WEB_DIR}/.env"
  else
    touch "${WEB_DIR}/.env"
  fi
  echo "Created ${WEB_DIR}/.env — edit SITE_SECRET and passwords!"
fi

grep -q '^SITE_DATA_DIR=' "${WEB_DIR}/.env" || echo "SITE_DATA_DIR=${DATA_DIR}" >> "${WEB_DIR}/.env"
grep -q '^SITE_PUBLIC_URL=' "${WEB_DIR}/.env" || echo "SITE_PUBLIC_URL=https://${DOMAIN}" >> "${WEB_DIR}/.env"
grep -q '^SITE_COOKIE_SECURE=' "${WEB_DIR}/.env" || echo "SITE_COOKIE_SECURE=1" >> "${WEB_DIR}/.env"
grep -q '^SITE_HOST=' "${WEB_DIR}/.env" || echo "SITE_HOST=127.0.0.1" >> "${WEB_DIR}/.env"
grep -q '^SITE_PORT=' "${WEB_DIR}/.env" || echo "SITE_PORT=3000" >> "${WEB_DIR}/.env"

if grep -q 'please-generate-a-long-random-string-here\|change-me-please' "${WEB_DIR}/.env" 2>/dev/null; then
  SEC="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  sed -i "s/^SITE_SECRET=.*/SITE_SECRET=${SEC}/" "${WEB_DIR}/.env" || echo "SITE_SECRET=${SEC}" >> "${WEB_DIR}/.env"
  echo "Generated new SITE_SECRET in .env"
fi

# Restore SSH deploy keys
if [[ -f "/tmp/exsender-deploy-key-backup.$$" ]]; then
  cp -a "/tmp/exsender-deploy-key-backup.$$" "${WEB_DIR}/deploy_key"
  rm -f "/tmp/exsender-deploy-key-backup.$$"
fi
if [[ -f "/tmp/exsender-deploy-key-pub-backup.$$" ]]; then
  cp -a "/tmp/exsender-deploy-key-pub-backup.$$" "${WEB_DIR}/deploy_key.pub"
  rm -f "/tmp/exsender-deploy-key-pub-backup.$$"
fi

python3 -m venv "${WEB_DIR}/.venv"
"${WEB_DIR}/.venv/bin/pip" install --upgrade pip -q
"${WEB_DIR}/.venv/bin/pip" install -r "${WEB_DIR}/requirements.txt" -q

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_ROOT}"
chmod 600 "${WEB_DIR}/.env" 2>/dev/null || true
chmod 600 "${WEB_DIR}/deploy_key" 2>/dev/null || true
chmod 700 "${DATA_DIR}" 2>/dev/null || true

cp "${INSTALL_ROOT}/deploy/site/exsender.service" /etc/systemd/system/exsender.service
cp "${INSTALL_ROOT}/deploy/site/nginx-exsender.conf" /etc/nginx/sites-available/exsender

ln -sf /etc/nginx/sites-available/exsender /etc/nginx/sites-enabled/exsender
rm -f /etc/nginx/sites-enabled/default
nginx -t

systemctl daemon-reload
systemctl enable exsender
systemctl restart exsender
systemctl reload nginx

echo "==> HTTP ok. Requesting Let's Encrypt certificate..."
CERTBOT_EMAIL="admin@${DOMAIN}"
CERT_FAIL=0
if getent ahosts "www.${DOMAIN}" >/dev/null 2>&1; then
  certbot --nginx -d "${DOMAIN}" -d "www.${DOMAIN}" --non-interactive --agree-tos -m "${CERTBOT_EMAIL}" --redirect || CERT_FAIL=1
else
  echo "www.${DOMAIN} has no DNS — certificate for apex only."
  certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos -m "${CERTBOT_EMAIL}" --redirect || CERT_FAIL=1
fi
if [[ "${CERT_FAIL}" == "1" ]]; then
  echo "certbot failed — check DNS A for ${DOMAIN}, then run:"
  echo "  certbot --nginx -d ${DOMAIN}"
fi

systemctl restart exsender
echo "==> Done. Data dir: ${DATA_DIR}"
echo "==> Open https://${DOMAIN}/login"
