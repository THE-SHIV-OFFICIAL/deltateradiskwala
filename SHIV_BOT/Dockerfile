FROM python:3.12-slim

WORKDIR /app
COPY SHIV_BOT/requirements.txt ./SHIV_BOT/requirements.txt
RUN pip install --no-cache-dir -r SHIV_BOT/requirements.txt
COPY SHIV_BOT ./SHIV_BOT
RUN mkdir -p data

CMD ["python", "-m", "SHIV_BOT.SHIV_BOT"]
