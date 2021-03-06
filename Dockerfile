# References:
# https://stackoverflow.com/a/57886655/407054
# https://pythonspeed.com/docker/

# base

FROM python:3.9.1 as base

ENV PYTHONFAULTHANDLER=1 \
    PYTHONHASHSEED=random \
    PYTHONUNBUFFERED=1

WORKDIR /app

# builder

FROM base as builder

ENV PIP_DEFAULT_TIMEOUT=100 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VERSION=1.1.4

RUN pip install "poetry==$POETRY_VERSION"
RUN python -m venv /venv

COPY pyproject.toml poetry.lock ./
RUN poetry export --dev -f requirements.txt | /venv/bin/pip install -r /dev/stdin

COPY . .
RUN poetry build && /venv/bin/pip install dist/*.whl

# final

FROM base as final

LABEL org.opencontainers.image.source=https://github.com/sprt/skippex

VOLUME /data

# Path XDG_DATA_HOME doesn't exist.
# Variable XDG_RUNTIME_DIR isn't set.
ENV XDG_DATA_HOME=/data \
    XDG_RUNTIME_DIR=/run
RUN mkdir -p "$XDG_DATA_HOME" "$XDG_RUNTIME_DIR"

COPY docker-entrypoint.sh .
COPY ./pyproject.toml ./pyproject.toml
COPY --from=builder /venv /venv
COPY ./tests ./tests
RUN chmod +x docker-entrypoint.sh

ENTRYPOINT ["./docker-entrypoint.sh"]
