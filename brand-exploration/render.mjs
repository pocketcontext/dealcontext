import {createRequire} from 'node:module';
import {readdir,readFile,writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const root=path.dirname(fileURLToPath(import.meta.url));
const require=createRequire(path.resolve(root,'../../pocketcontext-website/package.json'));
const sharp=require('sharp');
for(const dir of await readdir(path.join(root,'dist/assets'))){
 const folder=path.join(root,'dist/assets',dir);
 for(const file of await readdir(folder)) if(file.endsWith('.svg'))
  await sharp(await readFile(path.join(folder,file))).png().toFile(path.join(folder,file.replace('.svg','.png')));
}
console.log('Rendered 900 PNG exports.');
