FROM python:3.13-slim
RUN pip install --no-cache-dir radicale==3.9.0
COPY tests/integration/radicale.conf /etc/radicale/config
COPY tests/integration/radicale.users /etc/radicale/users
CMD ["radicale", "--config", "/etc/radicale/config"]
