FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MODEL_PATH=/opt/model/xgboost_breast_cancer.json \
    OBS_ENDPOINT=https://obs.cn-north-4.myhuaweicloud.com \
    OBS_KEY=models/xgboost_breast_cancer.json

# esdk-obs-python pulls in requests/urllib3; the constraint is satisfied by
# what python:3.11-slim-bookworm already provides. Pin a recent version.
RUN python -m pip install --no-cache-dir \
    xgboost-cpu==3.2.0 \
    numpy==1.26.4 \
    Flask==3.1.3 \
    gunicorn==23.0.0 \
    esdk-obs-python==3.24.6

# Mirror the ModelArts image contract from modelarts_minimal/Dockerfile:
# UID 1000 / GID 100, plain user "ma-user" / group "ma-group".
RUN existing_group="$(getent group 100 | cut -d: -f1)" \
    && if [ -n "$existing_group" ] && [ "$existing_group" != "ma-group" ]; then \
        groupmod --new-name ma-group "$existing_group"; \
    elif [ -z "$existing_group" ]; then \
        groupadd --gid 100 ma-group; \
    fi \
    && useradd \
        --uid 1000 \
        --gid 100 \
        --create-home \
        --shell /usr/sbin/nologin \
        ma-user

WORKDIR /opt/app
COPY app.py /opt/app/app.py

# Baked-in fallback model (ADR-0001): the service always starts with this,
# then upgrades to the OBS copy whenever OBS is reachable and configured.
COPY model/xgboost_breast_cancer.json /opt/model/xgboost_breast_cancer.json

RUN chown -R 1000:100 /opt/app /opt/model
USER 1000:100

EXPOSE 8080

CMD ["gunicorn", "--bind=0.0.0.0:8080", "--workers=1", "--threads=2", "--timeout=60", "--access-logfile=-", "--error-logfile=-", "app:app"]
