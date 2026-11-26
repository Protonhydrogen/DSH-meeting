import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { homedir } from 'node:os';
import { existsSync } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import * as meetings from './index.js';

export const name = meetings.name;
export const inject = meetings.inject;
function runtimeRoot() {
  return process.env.SC_INSTALL_ROOT || (process.platform === 'win32'
    ? join(process.env.LOCALAPPDATA || join(homedir(), 'AppData', 'Local'), 'DSH-meeting')
    : join(process.env.XDG_DATA_HOME || join(homedir(), '.local', 'share'), 'DSH-meeting'));
}

export async function apply(ctx, config) {
  const runtime = runtimeRoot();
  if (config.configPath === join(runtime, 'config', 'meeting.yaml')) {
    await mkdir(dirname(config.configPath), { recursive: true });
    const example = await readFile(new URL('../../config/example.yaml', import.meta.url), 'utf8');
    const initial = example.replace('output_root: ./meetings', 'output_root: ' + JSON.stringify(join(runtime, 'meetings')));
    try { await writeFile(config.configPath, initial, { flag: 'wx' }); }
    catch (error) { if (error.code !== 'EEXIST') throw error; }
  }
  return meetings.apply(ctx, config);
}

// The market package includes the Python source alongside the native plugin.
// Explicit Cordis configuration takes precedence over environment defaults.
export const Config = {
  '~standard': {
    version: 1,
    vendor: 'dsh-meetings',
    validate(raw) {
      const runtime = runtimeRoot();
      const projectRoot = process.env.DSH_MEETINGS_PROJECT_ROOT || fileURLToPath(new URL('../../', import.meta.url));
      const localConfig = join(projectRoot, 'config', 'local.yaml');
      return meetings.Config['~standard'].validate({
        projectRoot,
        python: process.env.DSH_MEETINGS_PYTHON || (process.platform === 'win32'
          ? join(runtime, 'venv', 'Scripts', 'python.exe')
          : join(runtime, 'venv', 'bin', 'python')),
        configPath: process.env.DSH_MEETINGS_CONFIG || (existsSync(localConfig) ? localConfig : join(runtime, 'config', 'meeting.yaml')),
        stateDir: join(runtime, 'state', 'dsh-meetings'),
        appRoot: join(process.resourcesPath || join(dirname(process.execPath), 'resources'), 'app.asar'),
        ...raw,
      });
    },
  },
};
