import electronUpdater from "electron-updater";

const { autoUpdater } = electronUpdater;
const CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;
const INITIAL_CHECK_DELAY_MS = 10 * 1000;

export class UpdateManager {
  constructor({ enabled, currentVersion, record, onChange = () => {}, beforeInstall = () => {} }) {
    this.enabled = Boolean(enabled);
    this.record = record;
    this.onChange = onChange;
    this.beforeInstall = beforeInstall;
    this.started = false;
    this.checking = null;
    this.initialTimer = null;
    this.interval = null;
    this.state = {
      enabled: this.enabled,
      status: this.enabled ? "idle" : "unavailable",
      currentVersion,
      availableVersion: null,
      percent: null,
      error: null,
    };
  }

  getState() {
    return { ...this.state };
  }

  update(values) {
    Object.assign(this.state, values);
    try { this.onChange(this.getState()); } catch {}
  }

  write(level, event, message, meta = {}) {
    try { return Promise.resolve(this.record({ level, event, message, meta })).catch(() => {}); }
    catch { return Promise.resolve(); }
  }

  start() {
    if (this.started || !this.enabled) return;
    this.started = true;
    autoUpdater.autoDownload = true;
    autoUpdater.autoInstallOnAppQuit = true;

    autoUpdater.on("checking-for-update", () => {
      this.update({ status: "checking", error: null, percent: null });
      this.write("info", "update.checking", "Buscando actualizaciones en GitHub");
    });
    autoUpdater.on("update-available", (info) => {
      this.update({ status: "downloading", availableVersion: info.version, error: null, percent: 0 });
      this.write("info", "update.available", `Actualización ${info.version} disponible; iniciando descarga`, { version: info.version });
    });
    autoUpdater.on("download-progress", (progress) => {
      this.update({ status: "downloading", percent: Math.max(0, Math.min(100, progress.percent)) });
    });
    autoUpdater.on("update-not-available", (info) => {
      this.update({ status: "current", availableVersion: null, error: null, percent: null });
      this.write("info", "update.current", "La aplicación está actualizada", { version: info.version });
    });
    autoUpdater.on("update-downloaded", (info) => {
      this.update({ status: "downloaded", availableVersion: info.version, error: null, percent: 100 });
      this.write("info", "update.downloaded", `Actualización ${info.version} descargada y lista para instalar`, { version: info.version });
    });
    autoUpdater.on("error", (error) => {
      const message = error?.message || String(error);
      this.update({ status: "error", error: message, percent: null });
      this.write("error", "update.error", `No se pudo actualizar: ${message}`);
    });

    this.initialTimer = setTimeout(() => this.check(), INITIAL_CHECK_DELAY_MS);
    this.initialTimer.unref?.();
    this.interval = setInterval(() => this.check(), CHECK_INTERVAL_MS);
    this.interval.unref?.();
  }

  async check({ manual = false } = {}) {
    if (!this.enabled) return this.getState();
    if (this.checking || ["checking", "downloading", "downloaded"].includes(this.state.status)) return this.getState();
    if (manual) await this.write("info", "update.manual", "Búsqueda manual de actualizaciones solicitada");
    try {
      this.checking = autoUpdater.checkForUpdates();
      await this.checking;
    } catch (error) {
      if (this.state.status !== "error") {
        const message = error?.message || String(error);
        this.update({ status: "error", error: message, percent: null });
        await this.write("error", "update.error", `No se pudo buscar actualizaciones: ${message}`);
      }
    } finally {
      this.checking = null;
    }
    return this.getState();
  }

  async install() {
    if (!this.enabled || this.state.status !== "downloaded") return false;
    this.beforeInstall();
    await this.write("info", "update.installing", `Instalando actualización ${this.state.availableVersion}`);
    setImmediate(() => autoUpdater.quitAndInstall(false, true));
    return true;
  }

  stop() {
    if (this.initialTimer) clearTimeout(this.initialTimer);
    if (this.interval) clearInterval(this.interval);
  }
}
