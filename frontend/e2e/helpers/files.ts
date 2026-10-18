import { readFileSync as readNative, type PathLike } from 'node:fs'
/** Wait for the actual file during cross-VM virtiofs rename invalidation. */
export function readFileSync(path: PathLike, encoding: 'utf8'): string
export function readFileSync(path: PathLike): Buffer
export function readFileSync(path: PathLike, encoding?: 'utf8'): string | Buffer {
  const deadline = Date.now() + 2000
  for (;;) {
    try {
      if (!encoding) return readNative(path)
      const text = readNative(path, encoding)
      if (String(path).endsWith('.json')) JSON.parse(text)
      return text
    } catch (error) {
      const transient = error instanceof SyntaxError || (error && typeof error === 'object' && 'code' in error && error.code === 'ENOENT')
      if (!transient || Date.now() >= deadline) throw error
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 20)
    }
  }
}
