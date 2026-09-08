const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("ikiway", {
  getState: () => ipcRenderer.invoke("agent:get-state"),
  getStatus: () => ipcRenderer.invoke("agent:get-status"),
  getLogs: () => ipcRenderer.invoke("agent:get-logs"),
  refreshPrinters: () => ipcRenderer.invoke("agent:list-printers"),
  selectPrinter: (key) => ipcRenderer.invoke("agent:select-printer", key),
  testPrint: () => ipcRenderer.invoke("agent:test-print"),
  saveOrigins: (origins) => ipcRenderer.invoke("agent:save-origins", origins),
  setStartup: (enabled) => ipcRenderer.invoke("agent:set-startup", enabled),
});
