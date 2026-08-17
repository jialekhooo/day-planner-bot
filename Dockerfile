FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PLANNER_DB=/data/planner.sqlite3
VOLUME ["/data"]
EXPOSE 8000

CMD ["sh", "-c", "uvicorn planner.web:app --host 0.0.0.0 --port ${PORT:-8000}"]
