FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock \
    && groupadd -g 10001 lab && useradd -u 10001 -g lab -M lab \
    && mkdir -p /data /secrets \
    && chown -R lab:lab /data /secrets
COPY . /app
USER 10001:10001
CMD ["python", "-m", "uvicorn", "services.bank.app:app", "--host", "0.0.0.0", "--port", "8080"]
