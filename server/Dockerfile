FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY relay.py .

EXPOSE 8080
CMD ["python", "-m", "uvicorn", "relay:app", "--host", "0.0.0.0", "--port", "8080", "--ws-max-size", "157286400"]
