# Pygmy server (the app, the printers, the MCP endpoint). Stdlib Python plus three small packages.
FROM python:3.11-slim
COPY deploy/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm -f /tmp/requirements.txt
COPY src/pygmy/ /app/
COPY profiles/ /app/profiles/
ENV STATE_DIRECTORY=/var/lib/pygmy PYGMY_MCP_PROFILES=/app/profiles PYTHONUNBUFFERED=1
VOLUME /var/lib/pygmy
CMD ["python", "/app/server.py"]
