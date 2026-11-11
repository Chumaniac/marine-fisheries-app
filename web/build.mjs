import {readFile,writeFile} from 'node:fs/promises';
import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';
await build({absWorkingDir:fileURLToPath(new URL('.',import.meta.url)),entryPoints:['src/app.js'],bundle:true,minify:true,outfile:'app.js',target:['es2022'],legalComments:'linked'});

const legalFile=new URL('./app.js.LEGAL.txt',import.meta.url);
await writeFile(legalFile,(await readFile(legalFile,'utf8')).replace(/[ \t]+$/gm,''));
