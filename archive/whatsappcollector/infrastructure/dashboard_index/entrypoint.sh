#!/bin/sh
set -e
# Substitute env vars into template and write to nginx html root
envsubst < /tmpl/index.html.tmpl > /usr/share/nginx/html/index.html
exec nginx -g 'daemon off;'
