#!/bin/bash
set -e

DOMAIN="orchestrator.suyogjoshi.com"
EMAIL="suyog19@gmail.com"

echo "==> Installing nginx and certbot"
apt-get update
apt-get install -y nginx certbot python3-certbot-nginx

echo "==> Writing nginx config for $DOMAIN"
cat > /etc/nginx/sites-available/orchestrator <<EOF
server {
    listen 80;
    server_name $DOMAIN;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

ln -sf /etc/nginx/sites-available/orchestrator /etc/nginx/sites-enabled/orchestrator
rm -f /etc/nginx/sites-enabled/default

nginx -t
systemctl reload nginx

echo "==> Obtaining SSL certificate"
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$EMAIL"

echo ""
echo "Done. Test with:"
echo "  curl https://$DOMAIN/healthz"
