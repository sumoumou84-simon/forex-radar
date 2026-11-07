FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY . .

# Hugging Face Spaces 默认通信端口
EXPOSE 7860
ENV PORT=7860

# 启动 24/7 外汇雷达与心跳服务
CMD ["python", "forex_monitor.py"]
