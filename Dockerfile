# APIバックエンド(api_server.py)専用コンテナ。
# フロントエンド(eco-digest-spark)・PMOレビュー用Streamlitアプリは別デプロイ（DEPLOY_GUIDE.md参照）。
FROM python:3.11-slim

WORKDIR /app

COPY requirements_azure.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV API_PORT=8000
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["python", "docker_entrypoint.py"]
