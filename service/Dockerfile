FROM python:3.12-slim

WORKDIR /app

# Install gh CLI
RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y gh && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY forksync/ ./forksync/
COPY service/ ./service/
COPY fork-sync.yml .

ENV PORT=8080
CMD ["uvicorn", "service.main:app", "--host", "0.0.0.0", "--port", "8080"]
