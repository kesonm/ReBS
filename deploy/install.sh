#!/usr/bin/env bash
# Run with sudo: bash install.sh RELEASE_ID /tmp/rebs-RELEASE_ID.tar.gz
set -euo pipefail
release_id="${1:?release id required}"
archive="${2:?archive path required}"
[[ "$release_id" =~ ^[a-zA-Z0-9_-]+$ ]] || { echo 'Invalid release ID' >&2; exit 1; }
[[ "$archive" == "/tmp/rebs-${release_id}.tar.gz" && -f "$archive" ]] || { echo 'Unexpected archive path' >&2; exit 1; }
release_dir="/opt/rebs/releases/${release_id}"
[[ ! -e "$release_dir" ]] || { echo 'Release already exists; choose a new ID' >&2; exit 1; }

install -d -m 0755 /opt/rebs/releases
mkdir -m 0755 "$release_dir"
tar -xzf "$archive" -C "$release_dir" --no-same-owner
python3 -m venv "$release_dir/.venv"
"$release_dir/.venv/bin/python" -m pip install --disable-pip-version-check -q -r "$release_dir/requirements.txt"
cd "$release_dir"
"$release_dir/.venv/bin/python" -m unittest discover -s tests -v

if ! id rebs >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/rebs --shell /usr/sbin/nologin rebs
fi
install -d -o rebs -g rebs -m 0750 /var/lib/rebs /var/cache/rebs
ln -s /var/cache/rebs "$release_dir/.cache"
install -d -o root -g rebs -m 0750 /etc/rebs
if [[ ! -e /etc/rebs/rebs.env ]]; then
    install -o root -g rebs -m 0640 /dev/null /etc/rebs/rebs.env
fi

previous_release="$(readlink -f /opt/rebs/current 2>/dev/null || true)"
install -m 0644 "$release_dir/deploy/rebs.service" /etc/systemd/system/rebs.service
install -m 0644 "$release_dir/deploy/nginx.conf" /etc/nginx/sites-available/rebs
if [[ -L /etc/nginx/sites-enabled/default && "$(readlink -f /etc/nginx/sites-enabled/default)" == /etc/nginx/sites-available/default ]]; then
    unlink /etc/nginx/sites-enabled/default
fi
ln -sfn /etc/nginx/sites-available/rebs /etc/nginx/sites-enabled/rebs
ln -sfn "$release_dir" /opt/rebs/current

rollback() {
    echo 'Deployment validation failed.' >&2
    if [[ "$previous_release" == /opt/rebs/releases/* && -d "$previous_release" ]]; then
        ln -sfn "$previous_release" /opt/rebs/current
        systemctl restart rebs || true
        echo "Restored previous release: $previous_release" >&2
    else
        systemctl stop rebs || true
    fi
}
trap rollback ERR
nginx -t
systemctl daemon-reload
systemctl enable rebs nginx
systemctl restart rebs
ready=false
for attempt in $(seq 1 30); do
    if curl -fsS --max-time 2 http://127.0.0.1:18080/api/health >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 1
done
[[ "$ready" == true ]]
systemctl reload nginx
proxy_ready=false
for attempt in $(seq 1 15); do
    if curl -fsS --max-time 3 http://127.0.0.1/api/health >/dev/null 2>&1; then
        proxy_ready=true
        break
    fi
    sleep 1
done
[[ "$proxy_ready" == true ]]
curl -fsS --max-time 5 http://127.0.0.1/api/health
trap - ERR
printf '\nDEPLOYED_RELEASE=%s\n' "$release_id"
printf 'Current release: %s\n' "$(readlink -f /opt/rebs/current)"
systemctl is-active rebs nginx
