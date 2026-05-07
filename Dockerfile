FROM python:3.12

LABEL Author="Luis Mex"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN mkdir /code
WORKDIR /code

COPY ./requirements.txt /code

RUN apt update && apt install -y postgresql-client ffmpeg
RUN pip install --upgrade pip && pip install -r requirements.txt yt-dlp

RUN mkdir -p /var/log/gunicorn
RUN chown www-data:www-data /var/log/gunicorn
RUN chmod 755 /var/log/gunicorn

COPY . /code
RUN ["chmod", "+x", "docker-entrypoint.sh"]
ENTRYPOINT ["bash", "/docker-entrypoint.sh"]
