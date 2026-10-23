import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const filename = process.env.WECHAT_CONFIG || path.join(root, 'runtime-local.json');
let settings = {};
try { settings = JSON.parse(fs.readFileSync(filename, 'utf8')); }
catch (error) { if (error.code !== 'ENOENT') throw error; }
if (!settings || Array.isArray(settings) || typeof settings !== 'object') {
  throw Error('Runtime configuration must be a JSON object');
}
export function setting(name, fallback) {
  return process.env['WECHAT_' + name.toUpperCase()] ?? settings[name] ?? fallback;
}
export const testApp = setting('test_app', path.join(os.homedir(), 'Applications/WeChat REA Test.app'));
export const dataRoot = setting('data_root', path.join(os.homedir(), 'Library/Containers/com.tencent.xinWeChat/Data'));
export const cache = setting('cache', path.join(dataRoot, 'Library/Caches/REAResearch'));
