// Keep Pi's tools and results; only replace their I/O for an explicit instance.
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
const require = createRequire(`${process.env.EVO_PI_RUNTIME}/package.json`);
const { Type } = require('typebox');
const COMMAND_TIMEOUT = 600;
const { createReadToolDefinition, createWriteToolDefinition, createEditToolDefinition,
  createBashToolDefinition } = await import(
  `${process.env.EVO_PI_RUNTIME}/node_modules/@earendil-works/pi-coding-agent/dist/index.js`);

export function codingTools(hostCall) {
  return [createReadToolDefinition, createWriteToolDefinition,
    createEditToolDefinition, createBashToolDefinition].map(factory => {
    const local = factory('/workspace');
    const shell = local.name === 'bash';
    return { ...local,
      name: shell ? 'exec' : local.name,
      label: shell ? 'Exec' : local.label,
      ...(shell ? { promptSnippet: 'Execute a command in the sandbox or selected instance.' } : {}),
      description: shell
        ? 'Run a one-shot command. Omit instance for sandbox /workspace; otherwise select an exact ID from remote_control list. '
          + 'The instance selects Bash on Linux or PowerShell 5.1 on Windows. timeout is seconds, default/max 600. '
          + 'Linux root=true requires reason and fresh Discord approval; root commands max 1000 chars. Windows has no elevation.'
        : local.description + ' Omit instance for /workspace; remote targets require an exact instance ID from remote_control list. '
          + 'With instance, paths must be absolute (or ~/ on SSH).',
      parameters: Type.Object({ ...local.parameters.properties,
        instance: Type.Optional(Type.String({ maxLength: 48 })),
        ...(shell ? { timeout: Type.Optional(Type.Number({ minimum: 0.1, maximum: COMMAND_TIMEOUT, default: COMMAND_TIMEOUT, description: 'Seconds; default/max 600. Longer work uses terminal sessions.' })),
          root: Type.Optional(Type.Boolean()),
          reason: Type.Optional(Type.String({ maxLength: 160 })) } : {}),
      }, { additionalProperties: false }),
      async execute(id, args, signal, onUpdate, ctx) {
        const { instance, root = false, reason, ...input } = args;
        if (shell && input.timeout === undefined) input.timeout = COMMAND_TIMEOUT;
        if (instance === undefined) {
          if (root || reason !== undefined) throw new Error('Use instance=localhost for host/root execution.');
          if (!shell && /^[A-Za-z]:[\\/]/.test(input.path ?? ''))
            throw new Error('Windows paths require instance: use remote_control list, then read/write/edit with that Windows instance.');
          return local.execute(id, input, signal, onUpdate, ctx);
        }
        if (typeof instance !== 'string' || !/^[A-Za-z0-9_-]{1,48}$/.test(instance))
          throw new Error('Choose an exact instance from remote_control list.');
        const call = async (name, request) => {
          const reply = await hostCall({ name, displayName: shell ? 'exec' : local.name }, { instance, ...request }, signal);
          const text = reply.content?.find(b => b.type === 'text')?.text;
          const result = JSON.parse(text);
          if (result.error || result.success === false && !('exit_code' in result)) throw new Error(result.error ?? 'Remote operation failed');
          return result;
        };
        let tool;
        if (shell) {
          if (root && (typeof reason !== 'string' || !reason.trim()))
            throw new Error('root=true requires a non-empty reason for Discord approval.');
          tool = factory('/', { exposeSessionEnvironment: false, operations: {
            exec: async (command, _cwd, { onData, timeout }) => {
              const result = await call('_pi_exec', { command, root,
                ...(root ? { reason } : {}), ...(timeout === undefined ? {} : { timeout }) });
              onData(Buffer.from(result.output ?? ''));
              if (result.timed_out || result.outcome === 'timeout') {
                onData(Buffer.from('\n[Remote command timed out; inspect before retrying. No automatic replay.]\n'));
                throw new Error(`timeout:${timeout}`);
              }
              if (result.outcome === 'cancelled') throw new Error('aborted');
              if (result.output_truncated) onData(Buffer.from('\n[Remote output was truncated.]'));
              return { exitCode: result.exit_code };
            },
          } });
        } else {
          let loaded;
          const load = async () => loaded ??= await call('_pi_io', { action: 'read', path: input.path });
          const operations = {
            access: async () => { await load(); },
            readFile: async () => Buffer.from((await load()).data, 'base64'),
            detectImageMimeType: async () => (await load()).mime,
            mkdir: async () => {}, // The selected transport creates parents on write.
            writeFile: async (_path, content) => {
              await call('_pi_io', { action: 'write', path: input.path, content,
                ...(loaded ? { expected: createHash('sha256').update(Buffer.from(loaded.data, 'base64')).digest('hex') } : {}) });
            },
          };
          tool = factory('/', { operations });
        }
        // Pi resolves paths and owns validation, edits, diff, truncation and cancellation.
        // The I/O closure retains the real path, including Windows paths and ~/ expansion.
        return tool.execute(id, input, signal, onUpdate, { ...ctx, cwd: `/__evo_remote__/${instance}` });
      },
    };
  });
}
