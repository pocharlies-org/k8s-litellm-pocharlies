# Claude subscription bridge (experimental)

OpenAI-compatible, multi-account bridge from LiteLLM to the official Claude
Agent SDK and Claude Code subscription credentials.

This is deliberately not a general-purpose Claude Code server:

- every model id names its account (`opus@personal`);
- requests require a generated bearer credential;
- the complete chat history is supplied on every turn and no server-side
  session is resumed;
- Agent SDK tools, skills, project settings and filesystem access are disabled;
- OpenAI tool calling currently fails closed with HTTP 400;
- each account admits one turn at a time and returns HTTP 429 when busy.

## Anthropic approval gate

Anthropic's Agent SDK documentation says that third parties may not offer
`claude.ai` login or subscription rate limits unless previously approved. The
bridge is therefore an explicit experimental canary. Do not add these models to
automatic fallbacks or offer them to other users without recorded Anthropic
approval. API-key-backed Claude in LiteLLM remains the supported production
path.

## Authentication bootstrap

The Kubernetes deployment owns one persistent Claude home per account. After
the canary is applied, authenticate each home independently:

```sh
kubectl -n litellm exec -it deploy/claude-subscription-bridge -- \
  env CLAUDE_CONFIG_DIR=/accounts/personal claude auth login

kubectl -n litellm exec -it deploy/claude-subscription-bridge -- \
  env CLAUDE_CONFIG_DIR=/accounts/tercera claude auth login

kubectl -n litellm exec -it deploy/claude-subscription-bridge -- \
  env CLAUDE_CONFIG_DIR=/accounts/works-shared claude auth login
```

One OAuth approval is required for each account. The persistent volumes, not a
Kubernetes Secret, own Claude's rotating credential files. Keep the bridge at
one replica: two processes refreshing the same account home are unsupported.

## Local checks

```sh
npm ci
npm test
docker build -t claude-subscription-bridge:test .
```
