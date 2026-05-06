FROM python:3.12-alpine

WORKDIR /app

COPY scripts ./scripts
COPY data ./data
COPY assets ./assets
COPY README.md ./README.md

EXPOSE 8080

CMD ["python", "scripts/web_app.py"]
