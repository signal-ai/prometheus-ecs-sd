FROM python:3.8.3-slim-buster

RUN mkdir -p /prometheus/ecs && \
    chown -R nobody /prometheus

COPY requirements.txt /prometheus
COPY discoverecs.py /prometheus
RUN pip3 install -r /prometheus/requirements.txt

USER nobody

ENTRYPOINT ["python", "/prometheus/discoverecs.py", "--directory", "/prometheus/ecs"]
