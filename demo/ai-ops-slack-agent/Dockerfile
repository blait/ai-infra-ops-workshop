# Same image runs the Slack bot (default CMD) or the optional AgentCore HTTP entrypoint.
# AgentCore Runtime requires linux/arm64:  docker build --platform linux/arm64 -t aiops-slack-agent .
# claude-agent-sdk ships a platform wheel that bundles the Claude Code CLI (no Node.js needed).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    AIOPS_DATA_DIR=/app/var

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY aiops ./aiops
COPY runbooks ./runbooks
COPY fixtures ./fixtures
COPY config ./config

RUN useradd --create-home --uid 10001 aiops && mkdir -p /app/var && chown -R aiops /app/var
USER aiops

# Slack Socket Mode bot (outbound WebSocket only; no inbound port)
CMD ["python", "-m", "aiops.slack_app"]
# AgentCore Runtime (optional): override with
#   CMD ["python", "-m", "aiops.agentcore_app"]  and HOST=0.0.0.0 PORT=8080
