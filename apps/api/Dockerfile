# Python standard library only, so there is nothing to install - no
# requirements.txt, no pip step, no wheel cache. The image is the runtime
# plus this repo's own code.
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    JARVIS_ROOT=/app \
    JARVIS_WEB_DIR=/app/web \
    JARVIS_HOST=0.0.0.0 \
    JARVIS_PORT=8080

COPY apps/api/agent /app/agent
COPY apps/web/public /app/web

# memory/ and .tokens/ are the only things JARVIS writes; compose mounts
# them from the host so a rebuilt image never loses a credential or a fact.
RUN mkdir -p /app/memory /app/.tokens

EXPOSE 8080
CMD ["python3", "agent/main.py"]
