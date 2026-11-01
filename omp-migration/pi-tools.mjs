// Keep Pi's tools and results; only replace their I/O for an explicit instance.
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
const require = createRequire(`${process.env.EVO_PI_RUNTIME}/package.json`);
const { Type } = require('typebox');
const { createReadToolDefinition, createWriteToolDefinition, createEditToolDefinition,
  createBashToolDefinition } = await import(
  `${process.env.EVO_PI_RUNTIME}/node_modules/@earendil-works/pi-coding-agent/dist/index.js`);

export function codingTools(hostCall) {
  const factories = [createReadToolDefinition, createWriteToolDefinition,
    createEditToolDefinition, createBashToolDefinition];
  return factories.map(factory => {
    const local = factory('/workspace');
    const shell = local.name === 'bash';
    return { ...local,
      description: local.description + ' Omit instance for the local workspace; otherwise use an exact ID from remote_control list.'
        + (shell ? ' Each call is independent. SSH/EVO use sh; Windows uses PowerShell. root=true requires reason and fresh Discord approval.' : ''),
      parameters: Type.Object({ ...local.parameters.properties,
        instance: Type.Optional(Type.String({ maxLength: 48 })),
        ...(shell ? { root: Type.Optional(Type.Boolean()),
          reason: Type.Optional(Type.String({ maxLength: 160 })) } : {}),
      }, { additionalProperties: false }),
      async execute(id, args, signal, onUpdate, ctx) {
        const { instance, root = false, reason, ...input } = args;
        if (instance === undefined) {
          if (root || reason !== undefined) throw new Error('Use instance=evo-host for host/root execution.');
          return local.execute(id, input, signal, onUpdate, ctx);
        }
        if (typeof instance !== 'string' || !/^[A-Za-z0-9_-]{1,48}$/.test(instance))
          throw new Error('Choose an exact instance from remote_control list.');
        const call = async (name, request) => {
          const reply = await hostCall({ name }, { instance, ...request }, signal);
          const text = reply.content?.find(b => b.type === 'text')?.text;
          const result = JSON.parse(text);
          if (result.error || result.success === false) throw new Error(result.error ?? 'Remote operation failed');
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
              if (result.timed_out) throw new Error('Remote command timed out; inspect before retrying. No automatic replay.');
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
