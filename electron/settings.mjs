import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";

export function normalizeOrigins(value) {
  const entries = Array.isArray(value) ? value : String(value ?? "").split(/[\n,]/);
  const origins = [];
  for (const entry of entries) {
    const candidate = String(entry).trim();
    if (!candidate) continue;
    let parsed;
    try { parsed = new URL(candidate); } catch { throw new Error(`Origen inválido: ${candidate}`); }
    if (!["http:", "https:"].includes(parsed.protocol)) throw new Error(`Origen inválido: ${candidate}`);
    if (parsed.origin !== candidate.replace(/\/$/, "")) {
      throw new Error(`Usa sólo esquema, dominio y puerto: ${candidate}`);
    }
    if (!origins.includes(parsed.origin)) origins.push(parsed.origin);
  }
  if (!origins.length) throw new Error("Configura al menos un origen autorizado.");
  if (origins.length > 20) throw new Error("Se permiten hasta 20 orígenes autorizados.");
  return origins;
}

function validPrinterRef(value) {
  return value && Number.isInteger(value.vendorId) && Number.isInteger(value.productId) ? value : null;
}

export class SettingsStore {
  constructor(filePath, defaults) {
    this.filePath = filePath;
    this.data = {
      allowedOrigins: normalizeOrigins(defaults.allowedOrigins),
      selectedPrinter: null,
    };
  }

  async load() {
    try {
      const parsed = JSON.parse(await readFile(this.filePath, "utf8"));
      this.data = {
        allowedOrigins: normalizeOrigins(parsed.allowedOrigins ?? this.data.allowedOrigins),
        selectedPrinter: validPrinterRef(parsed.selectedPrinter),
      };
    } catch (error) {
      if (error.code !== "ENOENT") console.warn(`No se pudo leer la configuración: ${error.message}`);
    }
    return this.get();
  }

  get() {
    return structuredClone(this.data);
  }

  async update(changes) {
    this.data = {
      allowedOrigins: changes.allowedOrigins === undefined
        ? this.data.allowedOrigins
        : normalizeOrigins(changes.allowedOrigins),
      selectedPrinter: changes.selectedPrinter === undefined
        ? this.data.selectedPrinter
        : validPrinterRef(changes.selectedPrinter),
    };
    await mkdir(path.dirname(this.filePath), { recursive: true });
    const temporary = `${this.filePath}.tmp`;
    await writeFile(temporary, `${JSON.stringify(this.data, null, 2)}\n`, { mode: 0o600 });
    await rename(temporary, this.filePath);
    return this.get();
  }
}
