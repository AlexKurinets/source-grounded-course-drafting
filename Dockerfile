FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install .

COPY templates ./templates
ENV COURSEDRAFT_TEMPLATE_DIR=/app/templates \
    COURSEDRAFT_DATA_DIR=/data

CMD ["coursedraft", "api", "--host", "0.0.0.0"]
