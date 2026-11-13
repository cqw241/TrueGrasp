#!/bin/sh
# Dedicated TLS-ALPN-01 renewal; never restart a retired application gateway.
set -eu
umask 077
exec 9>/run/truegrasp-ip-tls.lock
restart_marker=/run/truegrasp-gateway-restart-needed
tls_dir=/etc/truegrasp/tls

install_pending_certificate() {
    if [ -f "$tls_dir/install-pending" ]; then
        install -m 600 "$tls_dir/privkey.pem.new" "$tls_dir/privkey.pem"
        install -m 644 "$tls_dir/fullchain.pem.new" "$tls_dir/fullchain.pem"
        rm -f "$tls_dir/install-pending"
        rm -f "$tls_dir/privkey.pem.new" "$tls_dir/fullchain.pem.new"
    fi
}

restore_gateway() {
    install_pending_certificate
    if [ -f "$restart_marker" ]; then
        systemctl start truegrasp-gateway.service
        rm -f "$restart_marker"
    fi
}

if [ "${1:-}" = --restore-gateway ]; then
    flock -w 5 9 || exit 1
    restore_gateway
    exit 0
fi
flock -n 9 || exit 0
trap restore_gateway EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
restore_gateway
if [ -s "$tls_dir/fullchain.pem" ] && openssl x509 -checkend 259200 -noout -in "$tls_dir/fullchain.pem"; then
    echo 'Certificate has more than 72 hours remaining; no renewal needed.'
    exit 0
fi
if systemctl is-active --quiet truegrasp-gateway.service; then
    : > "$restart_marker"
    systemctl stop truegrasp-gateway.service
fi
if ss -H -ltn 'sport = :443' | grep -q .; then
    echo 'Port 443 is occupied; refusing to stop another service.' >&2
    exit 1
fi
/opt/truegrasp/tools/lego run \
    --path /etc/truegrasp/acme --account-id agent-guard-ip \
    --accept-tos --server https://acme-v02.api.letsencrypt.org/directory \
    --domains 8.148.178.2 --profile shortlived --tls --renew-days 3 --no-random-sleep
install -m 600 /etc/truegrasp/acme/certificates/8.148.178.2.key "$tls_dir/privkey.pem.new"
install -m 644 /etc/truegrasp/acme/certificates/8.148.178.2.crt "$tls_dir/fullchain.pem.new"
openssl x509 -noout -checkend 86400 -in "$tls_dir/fullchain.pem.new"
# Check that the new public key matches before exposing the replacement pair.
cert_key=$(openssl x509 -in "$tls_dir/fullchain.pem.new" -pubkey -noout | openssl sha256)
private_key=$(openssl pkey -in "$tls_dir/privkey.pem.new" -pubout | openssl sha256)
[ "$cert_key" = "$private_key" ]
: > "$tls_dir/install-pending"
install_pending_certificate
