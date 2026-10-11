import { cp, mkdir, readdir, rm } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'

const source = fileURLToPath(new URL('../dist/', import.meta.url))
const destination = fileURLToPath(new URL('../pocketbase/pb_public/', import.meta.url))
// Check the build exists before removing stale public assets.
await readdir(source)
await mkdir(destination, { recursive: true })
for (const entry of await readdir(destination)) {
  if (entry !== '.gitkeep') await rm(join(destination, entry), { recursive: true, force: true })
}
await cp(source, destination, { recursive: true })
