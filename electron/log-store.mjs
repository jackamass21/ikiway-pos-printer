import { appendFile, mkdir, readFile, rename, stat, writeFile } from "node:fs/promises";
import path from "node:path";

function clean(value, limit = 240) {
  return String(value ?? "").replace(/[\r\n\t]+/g, " ").slice(0, limit);
}

export class LogStore {
  constructor(filePath, { maxBytes = 5 * 1024 * 1024, retainedEntries = 2000 } = {}) {
    this.filePath = filePath;
    this.maxBytes = maxBytes;
    this.retainedEntries = retainedEntries;
    this.pending = Promise.resolve();
  }

  append(entry) {
    const safeEntry = {
      timestamp: new Date().toISOString(),
      level: ["info", "warn", "error"].includes(entry.level) ? entry.level : "info",
      event: clean(entry.event, 80),
      message: clean(entry.message, 500),
      ...Object.fromEntries(
        Object.entries(entry.meta ?? {})
          .filter(([, value]) => value !== undefined && value !== null && value !== "")
          .map(([key, value]) => [clean(key, 60), clean(value, 200)]),
      ),
    };
    this.pending = this.pending.then(async () => {
      await mkdir(path.dirname(this.filePath), { recursive: true });
      await appendFile(this.filePath, `${JSON.stringify(safeEntry)}\n`, { mode: 0o600 });
      const details = await stat(this.filePath);
      if (details.size > this.maxBytes) await this.rotate();
    }).catch((error) => console.error(`No se pudo guardar el log: ${error.message}`));
    return this.pending;
  }

  async rotate() {
    const contents = await readFile(this.filePath, "utf8");
    const lines = contents.trim().split("\n").slice(-this.retainedEntries);
    const temporary = `${this.filePath}.tmp`;
    await writeFile(temporary, `${lines.join("\n")}\n`, { mode: 0o600 });
    await rename(temporary, this.filePath);
  }

  async list(limit = 250) {
    await this.pending;
    const safeLimit = Math.max(1, Math.min(500, Number(limit) || 250));
    try {
      const contents = await readFile(this.filePath, "utf8");
      return contents.trim().split("\n").slice(-safeLimit).reverse().flatMap((line) => {
        try { return [JSON.parse(line)]; } catch { return []; }
      });
    } catch (error) {
      if (error.code === "ENOENT") return [];
      throw error;
    }
  }
}
