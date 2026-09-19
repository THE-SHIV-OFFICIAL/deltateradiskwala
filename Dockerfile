FROM python:3.12-slim

WORKDIR /app

COPY SHIV_BOT/requirements.txt /app/SHIV_BOT/requirements.txt
RUN pip install --no-cache-dir -r /app/SHIV_BOT/requirements.txt

COPY SHIV_BOT /app/SHIV_BOT
RUN mkdir -p /app/data

CMD ["python", "-m", "SHIV_BOT.SHIV_BOT"]