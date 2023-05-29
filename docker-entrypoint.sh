#!/bin/sh
#python manage.py makemigrations  
#python manage.py migrate  
python manage.py collectstatic --no-input
#python manage.py runserver 
gunicorn bookproject.wsgi:application --timeout 120 --workers=3 --threads=3 --worker-connections=1000 --bind 0.0.0.0:8000