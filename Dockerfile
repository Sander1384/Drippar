FROM python:3.12-alpine

WORKDIR /app
ENV DRIPARR_VERSION=0.1.17

COPY scripts ./scripts
COPY data ./data
COPY assets ./assets
COPY ["Rabbit Emoji", "./Rabbit Emoji"]
COPY README.md ./README.md
COPY RELEASE_NOTES.md ./RELEASE_NOTES.md

EXPOSE 8080

CMD ["python", "scripts/web_app.py"]
