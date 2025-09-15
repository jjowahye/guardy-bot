# Dockerfile
FROM python:3.11

# 로그 지연 없이 출력
ENV PYTHONUNBUFFERED=1

# 작업 디렉터리
WORKDIR /app

# 파이썬 의존성 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 소스 복사
COPY . .

# 실행 커맨드
CMD ["python", "bot.py"]
