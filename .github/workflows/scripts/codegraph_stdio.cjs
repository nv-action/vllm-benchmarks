// Relay MCP traffic without changing bytes. Record only tool discovery and call outcomes.
const {spawn} = require('node:child_process');
const {appendFileSync} = require('node:fs');
const {createInterface} = require('node:readline');

const head = process.argv[2];
const statsFile = process.argv[3];
const names = new Set(['codegraph_explore', 'codegraph_node', 'codegraph_callers']);
const child = spawn('codegraph', ['serve', '--mcp', '--path', head, '--no-watch'], {
  cwd: head,
  stdio: ['pipe', 'pipe', 'inherit'],
});
const pending = new Map();

function record(event) {
  appendFileSync(statsFile, JSON.stringify({time_ms: Date.now(), ...event}) + '\n', {mode: 0o600});
}

function observe(stream, handle) {
  createInterface({input: stream, crlfDelay: Infinity}).on('line', line => {
    // MCP stdio sends one JSON-RPC message per line.
    if (line.startsWith('{')) handle(JSON.parse(line));
  });
}

observe(process.stdin, message => {
  if (message.method === 'tools/list') pending.set(message.id, 'list');
  if (message.method === 'tools/call' && names.has(message.params?.name)) {
    pending.set(message.id, message.params.name);
    record({event: 'call', tool: message.params.name});
  }
});
observe(child.stdout, message => {
  const name = pending.get(message.id);
  if (!name || message.method) return;
  pending.delete(message.id);
  if (name === 'list') {
    const listed = (message.result?.tools || []).map(tool => tool.name).filter(tool => names.has(tool));
    record({event: 'tools-listed', tools: listed});
  } else {
    record({event: 'result', tool: name, success: !message.error && !!message.result && !message.result.isError});
  }
});
process.stdin.pipe(child.stdin);
child.stdout.pipe(process.stdout);
child.stdin.on('error', error => { if (error.code !== 'EPIPE') throw error; });
process.on('SIGTERM', () => child.kill('SIGTERM'));
process.on('SIGINT', () => child.kill('SIGINT'));
child.on('exit', (code) => { process.stdin.destroy(); process.exitCode = code ?? 0; });
