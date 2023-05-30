FROM python:3.9

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

RUN apt-get update && apt-get upgrade -y && apt-get install postgresql-client gcc  musl-dev  -y
RUN wget https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -O /usr/local/bin/yt-dlp
RUN chmod a+rx /usr/local/bin/yt-dlp  # Make executable
#let pip install required packages
RUN  pip install --upgrade pip && pip install -r requirements.txt  
COPY . /code
RUN ["chmod", "+x", "docker-entrypoint.sh"]
ENTRYPOINT ["bash","/docker-entrypoint.sh"]
