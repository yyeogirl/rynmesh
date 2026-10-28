// Build an isolated, explicit UI for preparing synthetic legacy migration input.
import fs from 'node:fs/promises';
import ts from '../webapp/node_modules/typescript/lib/typescript.js';

const output = 'D:/code/rynmesh-ai-recovery-acceptance/legacy-fixture';
await fs.access('D:/code/rynmesh-ai-recovery-acceptance/.ai-recovery-fixture.json');
const input = await fs.readFile(new URL('../webapp/src/domain/llmConversationStore.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(input, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
await fs.mkdir(output, { recursive: true });
await fs.writeFile(`${output}/legacy-store.js`, compiled.outputText);
await fs.copyFile(new URL('legacy_browser_fixture.html', import.meta.url), `${output}/index.html`);
console.log('Built explicit legacy preparation UI for the isolated acceptance node.');
