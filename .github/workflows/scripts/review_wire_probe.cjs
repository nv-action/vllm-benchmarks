// Observe only the serialized configuration and tool-call IDs passed to fetch.
// Do not persist provider credentials, prompts, arguments, tool results or responses.
const fs = require('node:fs');

const filename = process.env.AI_REVIEW_PROOF_FILE || '/tmp/ai-review-wire-proof.json';
const graphNames = ['explore', 'node', 'callers'].map(name => 'mcp__codegraph__codegraph_' + name);
const knownCalls = new Set();
const repliedCalls = new Set();
const summary = {
  model_requests: 0,
  requests_with_graph_declarations: Object.fromEntries(graphNames.map(name => [name, 0])),
  requests_with_all_graph_declarations: 0,
  requests_with_graph_server_hint: 0,
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

  if (summary.model_requests === 0) summary.first_provider_ms = Date.now();
  summary.model_requests++;
  if (request.thinking?.type === 'enabled') summary.explicit_thinking_enabled++;
  if (request.reasoning_effort === 'high') summary.explicit_reasoning_high++;
  if (request.tools?.length === 1 && request.tools[0].function?.name === 'run_code') {
    summary.only_run_code_offered++;
  }
  const context = request.messages
    .filter(m => m.role === 'system' || m.role === 'developer')
    .map(m => typeof m.content === 'string' ? m.content : (m.content || []).map(c => c.text || '').join(''))
    .join('\n');
  const start = context.indexOf('interface ToolArgsMap {');
  const end = context.indexOf('interface ToolOutputMap {', start);
  const hasArgsMap = start >= 0 && end > start;
  const argsMap = hasArgsMap ? context.slice(start, end) : '';
  const outsideArgs = hasArgsMap ? context.slice(0, start) + context.slice(end) : context;
  const declared = graphNames.filter(name => argsMap.includes(name + ':'));
  for (const name of declared) summary.requests_with_graph_declarations[name]++;
  if (declared.length === graphNames.length) summary.requests_with_all_graph_declarations++;
  if (outsideArgs.includes('Codegraph is a SQLite knowledge graph') && outsideArgs.includes('PRIMARY TOOL')) {
    summary.requests_with_graph_server_hint++;
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
