const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("ikiway", {
  getState: () => ipcRenderer.invoke("agent:get-state"),
  getStatus: () => ipcRenderer.invoke("agent:get-status"),
  getLogs: () => ipcRenderer.invoke("agent:get-logs"),
  checkUpdates: () => ipcRenderer.invoke("agent:check-updates"),
  installUpdate: () => ipcRenderer.invoke("agent:install-update"),
  onUpdateState: (callback) => {
    const listener = (_event, state) => callback(state);
    ipcRenderer.on("agent:update-state", listener);
    return () => ipcRenderer.removeListener("agent:update-state", listener);
  },
  refreshPrinters: () => ipcRenderer.invoke("agent:list-printers"),
  selectPrinter: (key) => ipcRenderer.invoke("agent:select-printer", key),
  testPrint: () => ipcRenderer.invoke("agent:test-print"),
  saveOrigins: (origins) => ipcRenderer.invoke("agent:save-origins", origins),
  setStartup: (enabled) => ipcRenderer.invoke("agent:set-startup", enabled),
});
