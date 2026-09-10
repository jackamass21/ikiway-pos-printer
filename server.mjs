import "dotenv/config";
import { pathToFileURL } from "node:url";
import { createApp } from "./app.mjs";

export function allowedOriginsFromEnv() {
  return (process.env.ALLOWED_ORIGINS || "http://localhost:8000,http://127.0.0.1:8000")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
}

export async function startServer({
  host = "127.0.0.1",
  port = 17892,
  manager,
  allowedOrigins = allowedOriginsFromEnv(),
  printerOptions = {
    encoding: process.env.PRINTER_ENCODING || "cp850",
    codePage: Number(process.env.PRINTER_CODE_PAGE || 2),
  },
  logEvent = ({ level, message, meta }) => {
    const details = Object.keys(meta ?? {}).length ? ` ${JSON.stringify(meta)}` : "";
    console[level === "error" ? "error" : level === "warn" ? "warn" : "log"](`[${new Date().toISOString()}] ${message}${details}`);
  },
  interactive = false,
} = {}) {
  // USB uses a native module. Delay loading it until the server is actually
  // started so Electron can report an actionable startup error if Windows
  // cannot load that native dependency on a particular machine.
  if (!manager) {
    const { PrinterManager } = await import("./printer-manager.mjs");
    manager = new PrinterManager();
  }
  await manager.initialize({ interactive });
  const application = createApp({
    allowedOrigins,
    printerOptions,
    logEvent,
    status: () => manager.status(),
    print: (buffer, copies) => manager.print(buffer, copies),
  });
  const server = await new Promise((resolve, reject) => {
    const instance = application.listen(port, host, () => resolve(instance));
    instance.once("error", reject);
  });
  return { server, manager, url: `http://${host}:${server.address().port}` };
}

const isDirectRun = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isDirectRun) {
  try {
    const result = await startServer({ interactive: true });
    if (!result.manager.status().printer_connected) {
      console.warn("No hay una impresora USB seleccionada. Conecta una y reinicia el agente.");
    }
    console.log(`Ikiway POS Printer Agent: ${result.url}/health`);
  } catch (error) {
    console.error(`No se pudo iniciar el agente: ${error.message}`);
    process.exitCode = 1;
  }
}
