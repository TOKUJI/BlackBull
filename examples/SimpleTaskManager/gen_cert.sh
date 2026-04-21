#!/usr/bin/env bash
# Generate a self-signed TLS certificate for local HTTPS / HTTP/2 testing.
# Browsers will show a security warning; click "Advanced → Proceed" once.
#
# Usage:
#   chmod +x gen_cert.sh && ./gen_cert.sh
#   python app.py --cert cert.pem --key key.pem [--port 8443]
set -e
openssl req -x509 -newkey rsa:4096 \
  -keyout key.pem -out cert.pem \
  -days 365 -nodes \
  -subj '/CN=localhost' \
  -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1'
echo "Done. Run: python app.py --cert cert.pem --key key.pem"
