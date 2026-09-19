FROM python:3.12-slim

WORKDIR /app

# TgCrypto ko compile karne ke liye zaroori tools install kar rahe hain
RUN apt-get update && apt-get install -y gcc python3-dev build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY SHIV_BOT/requirements.txt /app/SHIV_BOT/requirements.txt
RUN pip install --no-cache-dir -r /app/SHIV_BOT/requirements.txt

COPY SHIV_BOT /app/SHIV_BOT
RUN mkdir -p /app/data

CMD ["python", "-m", "SHIV_BOT.SHIV_BOT"]
