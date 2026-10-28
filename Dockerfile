FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY wot_verify ./wot_verify
RUN groupadd --gid 1000 bot && useradd --uid 1000 --gid bot --no-create-home bot && mkdir /data && chown bot:bot /data
USER bot
EXPOSE 8080
CMD ["python", "-m", "wot_verify"]
