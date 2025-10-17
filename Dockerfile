FROM python:3.11

#maintainer
LABEL Author="Luis Mex"

# The enviroment variable ensures that the python output is set straight
# to the terminal with out buffering it first
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
#directory to store app source code
RUN mkdir /code

#switch to /app directory so that everything runs from here
WORKDIR /code

#copy the app code to image working directory
COPY ./requirements.txt /code

RUN apt update &&  apt install -y postgresql-client gcc  musl-dev  ffmpeg
RUN wget https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -O /usr/local/bin/yt-dlp
RUN chmod a+rx /usr/local/bin/yt-dlp  # Make executable
#let pip install required packages
RUN  pip install --upgrade pip && pip install -r requirements.txt  

RUN mkdir -p /var/log/gunicorn
RUN chown www-data:www-data /var/log/gunicorn
RUN chmod 755 /var/log/gunicorn

COPY . /code
RUN ["chmod", "+x", "docker-entrypoint.sh"]
ENTRYPOINT ["bash","/docker-entrypoint.sh"]
