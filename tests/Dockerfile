FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/son

RUN groupadd --system son && useradd --system --gid son --home-dir /opt/son son
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=son:son src ./src
COPY --chown=son:son migrations ./migrations
COPY --chown=son:son app.py ./

USER son
EXPOSE 8000 8501

