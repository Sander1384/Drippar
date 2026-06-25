FROM python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

WORKDIR /app
ENV DRIPARR_VERSION=0.2.1

COPY scripts ./scripts
COPY data ./data
COPY assets ./assets
COPY ["Rabbit Emoji", "./Rabbit Emoji"]
COPY README.md ./README.md
COPY RELEASE_NOTES.md ./RELEASE_NOTES.md

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).read()" || exit 1

CMD ["python", "scripts/web_app.py"]
