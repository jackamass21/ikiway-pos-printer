import "dotenv/config";
import {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  Menu,
  nativeImage,
  Tray,
} from "electron";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { buildReceipt } from "../receipt.mjs";
import { PrinterManager } from "../printer-manager.mjs";
import { allowedOriginsFromEnv, startServer } from "../server.mjs";
import { LogStore } from "./log-store.mjs";
import { normalizeOrigins, SettingsStore } from "./settings.mjs";

const directory = path.dirname(fileURLToPath(import.meta.url));
const hiddenLaunch = process.argv.includes("--hidden");
let window;
let tray;
let service;
let manager;
let settings;
let logs;
let quitting = false;

const hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) app.quit();

async function record(entry) {
  if (logs) return logs.append(entry);
  console[entry.level === "error" ? "error" : "log"](entry.message);
}

function testReceipt() {
  return {
    schema: "ikiway.receipt.v1",
    config: {
      paper_width: "80",
      print_copies: 1,
      ticket_header: "IKIWAY",
      ticket_footer: "Conexión USB correcta",
      show_change_on_receipt: false,
    },
    order: { id: "PRUEBA", branch_name: "Agente local", status: "paid", subtotal: 0, tax_total: 0, total: 0 },
    ticket: { ticket_number: "PRUEBA-USB", change_due: 0 },
    lines: [{ description: "PRUEBA DE IMPRESIÓN", quantity: 1, unit_price: 0, line_total: 0 }],
    payments: [],
    electronic_document: {
      is_electronic: false,
      legal_name: "IKIWAY POS PRINTER",
      type_label: "Comprobante de prueba",
      folio: "PRUEBA",
      issued_at: new Date().toLocaleString("es-CL"),
      pdf417_data_uri: "",
    },
  };
}

function startupEnabled() {
  return app.isPackaged && app.getLoginItemSettings({ path: process.execPath }).openAtLogin;
}

async function state({ includePrinters = true, includeLogs = true } = {}) {
  return {
    version: app.getVersion(),
    serviceUrl: service?.url ?? null,
    status: manager.status(),
    printers: includePrinters ? await manager.list() : undefined,
    settings: settings.get(),
    startup: startupEnabled(),
    logs: includeLogs ? await logs.list(100) : undefined,
    logPath: logs.filePath,
  };
}

function registerIpc() {
  ipcMain.handle("agent:get-state", () => state());
  ipcMain.handle("agent:get-status", () => state({ includePrinters: false, includeLogs: false }));
  ipcMain.handle("agent:get-logs", () => logs.list(250));
  ipcMain.handle("agent:list-printers", () => manager.list());
  ipcMain.handle("agent:select-printer", async (_event, key) => {
    if (typeof key !== "string" || key.length > 200) throw new Error("Identificador de impresora inválido.");
    try {
      await manager.select(key);
      await record({ level: "info", event: "printer.selected", message: "Impresora USB seleccionada", meta: manager.status().selected_printer });
      return state();
    } catch (error) {
      await record({ level: "error", event: "printer.error", message: `No se pudo seleccionar la impresora: ${error.message}` });
      throw error;
    }
  });
  ipcMain.handle("agent:test-print", async () => {
    await record({ level: "info", event: "print.test", message: "Prueba de impresión solicitada" });
    try {
      const buffer = buildReceipt(testReceipt());
      await manager.print(buffer, 1);
      await record({ level: "info", event: "print.test.success", message: "Prueba de impresión enviada correctamente" });
      return { ok: true };
    } catch (error) {
      await record({ level: "error", event: "print.test.error", message: `Error en prueba de impresión: ${error.message}` });
      throw error;
    }
  });
  ipcMain.handle("agent:save-origins", async (_event, value) => {
    const allowedOrigins = normalizeOrigins(value);
    await settings.update({ allowedOrigins });
    await record({ level: "info", event: "settings.origins", message: "Orígenes autorizados actualizados", meta: { origins: allowedOrigins.join(", ") } });
    return settings.get();
  });
  ipcMain.handle("agent:set-startup", (_event, enabled) => {
    if (!app.isPackaged) return { enabled: false, available: false };
    app.setLoginItemSettings({
      openAtLogin: Boolean(enabled),
      path: process.execPath,
      args: ["--hidden"],
    });
    const result = { enabled: startupEnabled(), available: true };
    record({ level: "info", event: "settings.startup", message: result.enabled ? "Inicio automático activado" : "Inicio automático desactivado" });
    return result;
  });
}

function showWindow() {
  if (!window) return;
  window.show();
  window.focus();
}

function createWindow() {
  window = new BrowserWindow({
    width: 820,
    height: 720,
    minWidth: 680,
    minHeight: 600,
    show: false,
    backgroundColor: "#f4f7ff",
    title: "Ikiway POS Printer",
    icon: path.join(directory, "../build/icon.png"),
    webPreferences: {
      preload: path.join(directory, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  window.removeMenu();
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", (event) => event.preventDefault());
  window.on("close", (event) => {
    if (!quitting) {
      event.preventDefault();
      window.hide();
    }
  });
  window.once("ready-to-show", () => {
    if (!hiddenLaunch) showWindow();
  });
  window.loadFile(path.join(directory, "index.html"));
}

function createTray() {
  const icon = nativeImage.createFromPath(path.join(directory, "../build/icon.png")).resize({ width: 24, height: 24 });
  tray = new Tray(icon);
  tray.setToolTip("Ikiway POS Printer");
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "Abrir Ikiway POS Printer", click: showWindow },
    { type: "separator" },
    {
      label: "Salir",
      click: () => {
        quitting = true;
        app.quit();
      },
    },
  ]));
  tray.on("double-click", showWindow);
}

async function bootstrap() {
  const userDataPath = app.getPath("userData");
  logs = new LogStore(path.join(userDataPath, "agent.jsonl"));
  const settingsPath = path.join(userDataPath, "settings.json");
  settings = new SettingsStore(settingsPath, { allowedOrigins: allowedOriginsFromEnv() });
  await settings.load();
  manager = new PrinterManager({
    selectedRef: settings.get().selectedPrinter,
    onSelectionChange: (selectedPrinter) => settings.update({ selectedPrinter }),
  });
  service = await startServer({
    manager,
    allowedOrigins: () => settings.get().allowedOrigins,
    logEvent: (entry) => record(entry),
  });
  registerIpc();
  createWindow();
  createTray();
  await record({ level: "info", event: "agent.started", message: `Agente iniciado en ${service.url}` });
}

if (hasSingleInstanceLock) {
  app.on("second-instance", showWindow);
  app.on("activate", showWindow);
  app.on("before-quit", () => { quitting = true; });
  app.on("will-quit", () => service?.server.close());
  app.whenReady().then(bootstrap).catch((error) => {
    record({ level: "error", event: "agent.error", message: `Error de inicio: ${error.stack || error.message}` });
    dialog.showErrorBox("Ikiway POS Printer", `No se pudo iniciar el agente:\n${error.message}`);
    quitting = true;
    app.quit();
  });
}
