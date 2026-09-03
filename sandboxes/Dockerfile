FROM node:20-slim

RUN npm install -g @anthropic-ai/claude-code \
    && groupadd --gid 1001 agentrunner \
    && useradd --uid 1001 --gid agentrunner --create-home --shell /bin/bash agentrunner

WORKDIR /home/agentrunner/workspace
RUN chown -R agentrunner:agentrunner /home/agentrunner/workspace

USER agentrunner

ENTRYPOINT ["claude-code"]
