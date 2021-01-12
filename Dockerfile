FROM python:3.8-slim

WORKDIR /usr/local/bin
ADD . $workdir

RUN pip install -r requirements.txt
