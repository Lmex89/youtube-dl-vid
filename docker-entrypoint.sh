#!/bin/sh
python manage.py makemigrations --no-input
python manage.py migrate --no-input
python manage.py collectstatic --no-input
gunicorn Youtube_dl_vid.wsgi:application --timeout 300 --workers=2 --threads=2 --worker-connections=200 --bind 0.0.0.0:8000
