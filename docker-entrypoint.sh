#!/bin/sh
#python manage.py makemigrations  
#python manage.py migrate  
python manage.py collectstatic --no-input
#python manage.py runserver 
gunicorn Youtube_dl_vid.wsgi:application --timeout 240 --workers=4 --threads=1 --worker-connections=2000 --bind 0.0.0.0:8000