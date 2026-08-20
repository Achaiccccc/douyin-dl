FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 国内 NAS 构建走阿里云 PyPI 镜像；在境外构建可用 --build-arg PIP_INDEX_URL=https://pypi.org/simple 覆盖
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt -i ${PIP_INDEX_URL} --extra-index-url https://pypi.org/simple

COPY app ./app
COPY static ./static
COPY crawlers ./crawlers

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
