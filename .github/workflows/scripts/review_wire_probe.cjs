// Observe only the serialized configuration and tool-call IDs passed to fetch.
// Do not persist provider credentials, prompts, arguments, tool results or responses.
const fs = require('node:fs');

const filename = process.env.AI_REVIEW_PROOF_FILE || '/tmp/ai-review-wire-proof.json';
const knownCalls = new Set();
const repliedCalls = new Set();
const summary = {
  model_requests: 0,
  explicit_thinking_enabled: 0,
  explicit_reasoning_high: 0,
  only_run_code_offered: 0,
  distinct_run_code_calls: 0,
  distinct_run_code_results_returned: 0,
};

function observe(body) {
  if (typeof body !== 'string') return;
  let request;
  try {
    request = JSON.parse(body);
  } catch {
    return;
  }
  if (request?.model !== 'deepseek-v4.1-flash' || !Array.isArray(request.messages)) return;

  summary.model_requests++;
  if (request.thinking?.type === 'enabled') summary.explicit_thinking_enabled++;
  if (request.reasoning_effort === 'high') summary.explicit_reasoning_high++;
  if (request.tools?.length === 1 && request.tools[0].function?.name === 'run_code') {
    summary.only_run_code_offered++;
  }
  for (const message of request.messages) {
    if (message.role === 'assistant') {
      for (const call of message.tool_calls || []) {
        if (call.function?.name === 'run_code' && call.id) knownCalls.add(call.id);
      }
    } else if (message.role === 'tool' && knownCalls.has(message.tool_call_id)) {
      repliedCalls.add(message.tool_call_id);
    }
  }
  summary.distinct_run_code_calls = knownCalls.size;
  summary.distinct_run_code_results_returned = repliedCalls.size;
  fs.writeFileSync(filename, JSON.stringify(summary) + '\n', {mode: 0o600});
}

const originalFetch = globalThis.fetch;
globalThis.fetch = function (...args) {
  observe(args[1]?.body);
  return Reflect.apply(originalFetch, this, args);
};
