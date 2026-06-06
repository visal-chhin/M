FROM python:3.13

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir \
    python-telegram-bot \
    requests \
    python-dotenv \
    curl_cffi

CMD ["python", "movie.py"]