#!/bin/bash
# Installs / updates Pi Backup on Raspberry Pi OS (Debian Bookworm or newer).
# Run from the project folder:  sudo bash install.sh
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "Please run with sudo:  sudo bash install.sh"
  exit 1
fi

SRC="$(cd "$(dirname "$0")" && pwd)"
APP=/opt/pi-backup
ENV_FILE=/etc/pi-backup.env

echo "==> Installing packages"
apt-get update
apt-get install -y python3-flask python3-waitress rclone cryptsetup ntfs-3g exfatprogs

echo "==> Copying the app to $APP"
mkdir -p "$APP/templates"
install -m 644 "$SRC/app.py" "$APP/app.py"
install -m 644 "$SRC/templates/index.html" "$APP/templates/index.html"
install -m 644 "$SRC/pi-backup.service" /etc/systemd/system/pi-backup.service
mkdir -p /var/lib/pi-backup /mnt/backup
chmod 700 /var/lib/pi-backup

if [ ! -f "$ENV_FILE" ]; then
  echo "==> Choose a password for the dashboard (any user name works when logging in)"
  read -r -s -p "Dashboard password: " PW; echo
  echo "==> Which computers may open the dashboard? IPs or networks, comma separated"
  echo "    (e.g. 192.168.100.21,192.168.100.22). Leave empty to allow any computer."
  read -r -p "Allowed IPs: " ALLOWED
  umask 077
  cat > "$ENV_FILE" <<EOF
# Pi Backup settings - restart after changes:  sudo systemctl restart pi-backup
PIB_PASSWORD=$PW
# Computers allowed to open the dashboard (comma separated IPs or networks, empty = any).
# The Pi itself is always allowed, so an SSH tunnel keeps working.
PIB_ALLOWED_IPS=$ALLOWED
PIB_PORT=8080
PIB_MOUNT=/mnt/backup
PIB_TRANSFERS=4
EOF
  chmod 600 "$ENV_FILE"
fi

echo "==> Starting the service"
systemctl daemon-reload
systemctl enable pi-backup
systemctl restart pi-backup

IP="$(hostname -I | awk '{print $1}')"
echo
echo "Done. Open  http://$IP:$(grep -oP '^PIB_PORT=\K.*' "$ENV_FILE" || echo 8080)  in a browser."
echo "rclone version: $(rclone version | head -n1)"
