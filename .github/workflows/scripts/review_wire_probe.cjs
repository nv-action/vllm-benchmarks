// Record counts from primary PTC requests. Keep prompts, responses, arguments,
// tool-call IDs and credentials out of the proof file.
const fs = require('node:fs');
const {performance} = require('node:perf_hooks');

const filename = process.env.AI_REVIEW_PROOF_FILE || '/tmp/ai-review-wire-proof.json';
const toolNames = ['bash', 'read', 'web_search', 'web_fetch'];
const clockHeader = /Time sampled while preparing turn (\d+), step (\d+): \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}\[[^\]\n]+\]/g;
const clocks = new Set();
const calls = new Set();
const replies = new Set();
const callsAfterClock = new Set();
const repliesAfterClock = new Set();
let started;
const summary = {
  model_requests: 0,
  only_run_code_offered: 0,
  explicit_thinking_enabled: 0,
  explicit_reasoning_high: 0,
  requests_with_ptc_args_map: 0,
  requests_with_tool_declaration: Object.fromEntries(toolNames.map(name => [name, 0])),
  distinct_clock_readings: 0,
  first_clock: null,
  second_clock: null,
  distinct_run_code_calls: 0,
  distinct_run_code_results_returned: 0,
  distinct_run_code_calls_after_second_clock: 0,
  distinct_run_code_results_after_second_clock: 0,
};

function messageText(message) {
  return typeof message.content === 'string' ? message.content :
    (message.content || []).map(block => block.text || '').join('\n');
}

function observe(body) {
  if (typeof body !== 'string') return;
  let request;
  try { request = JSON.parse(body); } catch { return; }
  if (request?.model !== 'deepseek-v4.1-flash' || !Array.isArray(request.messages) ||
      request.tools?.length !== 1 || request.tools[0].function?.name !== 'run_code') return;

  const now = performance.now();
  if (started === undefined) {
    started = now;
    summary.first_provider_ms = Date.now();
  }
  const index = ++summary.model_requests;
  summary.only_run_code_offered++;
  if (request.thinking?.type === 'enabled') summary.explicit_thinking_enabled++;
  if (request.reasoning_effort === 'high') summary.explicit_reasoning_high++;

  const context = request.messages
    .filter(message => message.role === 'system' || message.role === 'developer')
    .map(messageText).join('\n');
  const args = context.match(/interface ToolArgsMap \{([\s\S]*?)\n\}\s*interface ToolOutputMap \{/);
  if (args) {
    summary.requests_with_ptc_args_map++;
    // DSH indents top-level TypeScript declarations by exactly two spaces.
    for (const name of toolNames) {
      if (new RegExp('^  ' + name + ':', 'm').test(args[1])) summary.requests_with_tool_declaration[name]++;
    }
  }

  for (const message of request.messages) {
    if (message.role !== 'user') continue;
    for (const match of messageText(message).matchAll(clockHeader)) {
      const key = match[1] + ':' + match[2];
      if (clocks.has(key)) continue;
      clocks.add(key);
      const reading = {turn: Number(match[1]), step: Number(match[2]), request: index,
        elapsed_seconds: Math.round((now - started) / 100) / 10};
      if (clocks.size === 1) summary.first_clock = reading;
      if (clocks.size === 2) summary.second_clock = reading;
    }
  }
  summary.distinct_clock_readings = clocks.size;

  // The request that first carries clock #2 contains calls from *before* it.
  // A response to this request is visible only in a subsequent request history.
  const afterSecond = summary.second_clock !== null && index > summary.second_clock.request;
  for (const message of request.messages) {
    if (message.role === 'assistant') {
      for (const call of message.tool_calls || []) {
        if (call.function?.name !== 'run_code' || !call.id || calls.has(call.id)) continue;
        calls.add(call.id);
        if (afterSecond) callsAfterClock.add(call.id);
      }
    } else if (message.role === 'tool' && calls.has(message.tool_call_id)) {
      replies.add(message.tool_call_id);
      if (callsAfterClock.has(message.tool_call_id)) repliesAfterClock.add(message.tool_call_id);
    }
  }
  summary.distinct_run_code_calls = calls.size;
  summary.distinct_run_code_results_returned = replies.size;
  summary.distinct_run_code_calls_after_second_clock = callsAfterClock.size;
  summary.distinct_run_code_results_after_second_clock = repliesAfterClock.size;
  const temp = filename + '.tmp';
  fs.writeFileSync(temp, JSON.stringify(summary) + '\n', {mode: 0o600});
  fs.renameSync(temp, filename);
}

const originalFetch = globalThis.fetch;
globalThis.fetch = function (...args) {
  observe(args[1]?.body);
  return Reflect.apply(originalFetch, this, args);
};
