FROM node:22.18.0-bookworm-slim AS test

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY src ./src
COPY test ./test
RUN npm test && touch /tmp/claude-bridge-tests-passed

FROM node:22.18.0-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

COPY src ./src
COPY --from=test /tmp/claude-bridge-tests-passed /tmp/claude-bridge-tests-passed

ENV NODE_ENV=production \
    PATH=/app/node_modules/.bin:$PATH

USER 1000:1000
EXPOSE 8080
CMD ["node", "src/bridge.mjs"]
