# BFF server (the app, the printers, the MCP endpoint). Stdlib Python plus three small packages.
FROM python:3.11-slim
COPY deploy/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm -f /tmp/requirements.txt
COPY src/bff_mcp/ /app/
COPY profiles/ /app/profiles/
ENV STATE_DIRECTORY=/var/lib/bff-mcp BFF_MCP_PROFILES=/app/profiles PYTHONUNBUFFERED=1
VOLUME /var/lib/bff-mcp
CMD ["python", "/app/server.py"]
