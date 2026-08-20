import assert from "node:assert/strict";
import test from "node:test";
import {
  buildPrompt,
  buildSdkOptions,
  createBridgeServer,
  loadConfig,
  selectDeployment,
} from "../src/bridge.mjs";

const API_KEY = "test-key-that-is-longer-than-24-characters";

function config(overrides = {}) {
  return {
    apiKey: API_KEY,
    accounts: new Map([["personal", { id: "personal", configDir: "/accounts/personal" }]]),
    host: "127.0.0.1",
    port: 0,
    cwd: "/workspace",
    maxInflightPerAccount: 1,
    queryTimeoutMs: 1000,
    ...overrides,
  };
}

async function withServer(query, callback) {
  const server = createBridgeServer(config(), { query });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const { port } = server.address();
    await callback(`http://127.0.0.1:${port}`);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

test("configuration requires strong auth and explicit absolute account homes", () => {
  assert.throws(() => loadConfig({ CLAUDE_BRIDGE_API_KEY: "short", CLAUDE_BRIDGE_ACCOUNTS: "[]" }), /at least 24/);
  assert.throws(() => loadConfig({ CLAUDE_BRIDGE_API_KEY: API_KEY, CLAUDE_BRIDGE_ACCOUNTS: '[{"id":"personal","configDir":"relative"}]' }), /absolute/);
  const loaded = loadConfig({ CLAUDE_BRIDGE_API_KEY: API_KEY, CLAUDE_BRIDGE_ACCOUNTS: '[{"id":"personal","configDir":"/accounts/personal"}]' });
  assert.equal(loaded.accounts.get("personal").configDir, "/accounts/personal");
});

test("model selection always identifies an account", () => {
  const accounts = config().accounts;
  assert.throws(() => selectDeployment("opus", accounts), /must identify/);
  assert.equal(selectDeployment("openai/opus@personal", accounts).resolvedModel, "opus");
  assert.throws(() => selectDeployment("opus@missing", accounts), /unknown Claude deployment/);
});

test("SDK execution is stateless, strips API credentials, and disables every tool surface", () => {
  const deployment = selectDeployment("opus@personal", config().accounts);
  const options = buildSdkOptions(config(), deployment, { reasoning_effort: "high" }, {
    ANTHROPIC_API_KEY: "must-not-pass",
    CLAUDE_CODE_OAUTH_TOKEN: "must-not-pass",
    SAFE_ENV: "kept",
  });
  assert.equal(options.env.CLAUDE_CONFIG_DIR, "/accounts/personal");
  assert.equal(options.env.SAFE_ENV, "kept");
  assert.equal(options.env.ANTHROPIC_API_KEY, undefined);
  assert.equal(options.env.CLAUDE_CODE_OAUTH_TOKEN, undefined);
  assert.deepEqual(options.tools, []);
  assert.deepEqual(options.allowedTools, []);
  assert.deepEqual(options.settingSources, []);
  assert.deepEqual(options.skills, []);
  assert.equal(options.resume, undefined);
  assert.equal(options.effort, "high");
});

test("prompt includes the complete supplied history without server-side session state", () => {
  const prompt = buildPrompt([
    { role: "system", content: "Be concise" },
    { role: "user", content: "first" },
    { role: "assistant", content: "answer" },
    { role: "user", content: "follow-up" },
  ]);
  for (const value of ["Be concise", "first", "answer", "follow-up"]) assert.match(prompt, new RegExp(value));
});

test("health is minimal, inference is authenticated, and tools fail closed", async () => {
  await withServer(async function* () {}, async (base) => {
    const health = await fetch(`${base}/health`);
    assert.deepEqual(await health.json(), { ok: true });
    const unauth = await fetch(`${base}/v1/models`);
    assert.equal(unauth.status, 401);
    const tools = await fetch(`${base}/v1/chat/completions`, {
      method: "POST",
      headers: { authorization: `Bearer ${API_KEY}`, "content-type": "application/json" },
      body: JSON.stringify({ model: "opus@personal", messages: [{ role: "user", content: "hello" }], tools: [{ type: "function" }] }),
    });
    assert.equal(tools.status, 400);
    assert.equal((await tools.json()).error.code, "unsupported_tools");
  });
});

test("non-stream and stream responses use the OpenAI chat protocol", async () => {
  const query = async function* () {
    yield { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: "hello " } } };
    yield { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: "world" } } };
    yield { type: "result", usage: { input_tokens: 4, output_tokens: 2 } };
  };
  await withServer(query, async (base) => {
    const request = { model: "opus@personal", messages: [{ role: "user", content: "hello" }] };
    const response = await fetch(`${base}/v1/chat/completions`, {
      method: "POST",
      headers: { authorization: `Bearer ${API_KEY}`, "content-type": "application/json" },
      body: JSON.stringify(request),
    });
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.equal(body.choices[0].message.content, "hello world");
    assert.equal(body.usage.total_tokens, 6);

    const streamed = await fetch(`${base}/v1/chat/completions`, {
      method: "POST",
      headers: { authorization: `Bearer ${API_KEY}`, "content-type": "application/json" },
      body: JSON.stringify({ ...request, stream: true }),
    });
    const text = await streamed.text();
    assert.match(text, /hello /);
    assert.match(text, /world/);
    assert.match(text, /data: \[DONE\]/);
  });
});
