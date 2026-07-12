FROM python:3.11

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 카카오클라우드(서버리스) 환경에서 동적으로 할당하는 PORT 환경변수 지원
CMD uvicorn mcp_server:app --host 0.0.0.0 --port ${PORT:-8080}
