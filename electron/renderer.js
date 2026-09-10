(() => {
  "use strict";
  const elements = {
    badge: document.getElementById("service-badge"),
    printers: document.getElementById("printers"),
    summary: document.getElementById("printer-summary"),
    detail: document.getElementById("printer-detail"),
    refresh: document.getElementById("refresh"),
    test: document.getElementById("test-print"),
    origins: document.getElementById("origins"),
    saveOrigins: document.getElementById("save-origins"),
    startup: document.getElementById("startup"),
    updateSummary: document.getElementById("update-summary"),
    checkUpdates: document.getElementById("check-updates"),
    installUpdate: document.getElementById("install-update"),
    logs: document.getElementById("logs"),
    logSummary: document.getElementById("log-summary"),
    logPath: document.getElementById("log-path"),
    refreshLogs: document.getElementById("refresh-logs"),
    message: document.getElementById("message"),
    version: document.getElementById("version"),
  };

  function message(text, error = false) {
    elements.message.textContent = text;
    elements.message.classList.toggle("error", error);
  }

  function renderStatus(state) {
    const connected = state.status.printer_connected;
    elements.badge.textContent = connected ? "Agente activo" : "Sin impresora";
    elements.badge.className = `badge ${connected ? "ok" : "error"}`;
    elements.test.disabled = !connected;
    if (state.update) renderUpdate(state.update);
  }

  function renderUpdate(update) {
    if (!update) return;
    const version = update.availableVersion || update.currentVersion;
    const messages = {
      unavailable: "Las actualizaciones automáticas están disponibles en el instalador de Windows.",
      idle: `Versión ${update.currentVersion} · comprobación automática activa.`,
      checking: "Buscando actualizaciones en GitHub…",
      current: `Versión ${update.currentVersion} · aplicación actualizada.`,
      downloading: `Descargando versión ${version}${Number.isFinite(update.percent) ? ` · ${Math.round(update.percent)}%` : ""}.`,
      downloaded: `Versión ${version} lista. Reinicia para instalarla.`,
      error: `Error al buscar actualizaciones: ${update.error || "error desconocido"}`,
    };
    elements.updateSummary.textContent = messages[update.status] || messages.idle;
    elements.checkUpdates.disabled = !update.enabled || ["checking", "downloading"].includes(update.status);
    elements.installUpdate.hidden = update.status !== "downloaded";
  }

  function renderPrinters(printers) {
    elements.printers.replaceChildren();
    if (!printers.length) {
      const option = document.createElement("option");
      option.textContent = "No se detectaron impresoras ESC/POS USB";
      option.value = "";
      elements.printers.append(option);
      elements.printers.disabled = true;
      elements.summary.textContent = "Conecta la impresora por USB y pulsa Actualizar.";
      elements.detail.textContent = "";
      return;
    }
    elements.printers.disabled = false;
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = printers.length > 1 ? "Selecciona una impresora" : "Impresora detectada";
    elements.printers.append(placeholder);
    for (const printer of printers) {
      const option = document.createElement("option");
      option.value = printer.key;
      option.textContent = `${printer.product || "Impresora USB"} — ${printer.vendorId}:${printer.productId}`;
      option.selected = printer.selected;
      elements.printers.append(option);
    }
    const selected = printers.find((printer) => printer.selected);
    elements.summary.textContent = selected ? "Lista para recibir comprobantes de Ikiway." : "Selecciona la impresora que utilizará el POS.";
    elements.detail.textContent = selected
      ? `${selected.manufacturer || "Fabricante no informado"}${selected.portNumbers ? ` · Puerto ${selected.portNumbers}` : ""}`
      : `${printers.length} impresora(s) detectada(s)`;
  }

  function renderLogs(entries) {
    elements.logs.replaceChildren();
    elements.logSummary.textContent = `${entries.length} evento(s) reciente(s) · actualización automática`;
    if (!entries.length) {
      const empty = document.createElement("div");
      empty.className = "log-empty";
      empty.textContent = "Todavía no hay eventos registrados.";
      elements.logs.append(empty);
      return;
    }
    for (const entry of entries) {
      const row = document.createElement("div");
      row.className = `log-entry ${entry.level}`;
      const time = document.createElement("span");
      time.className = "log-time";
      const parsedDate = new Date(entry.timestamp);
      time.textContent = Number.isNaN(parsedDate.getTime()) ? entry.timestamp : parsedDate.toLocaleString("es-CL");
      const level = document.createElement("span");
      level.className = "log-level";
      level.textContent = entry.level === "error" ? "Error" : entry.level === "warn" ? "Aviso" : "Info";
      const content = document.createElement("span");
      content.className = "log-content";
      const description = document.createElement("strong");
      description.textContent = entry.message;
      content.append(description);
      const ignored = new Set(["timestamp", "level", "event", "message"]);
      const metadata = Object.entries(entry).filter(([key]) => !ignored.has(key));
      if (metadata.length) {
        const details = document.createElement("span");
        details.className = "log-meta";
        details.textContent = metadata.map(([key, value]) => `${key}: ${value}`).join(" · ");
        content.append(details);
      }
      row.append(time, level, content);
      elements.logs.append(row);
    }
  }

  async function refreshLogs() {
    try { renderLogs(await window.ikiway.getLogs()); } catch (error) { message(error.message, true); }
  }

  async function load() {
    try {
      const state = await window.ikiway.getState();
      renderStatus(state);
      renderPrinters(state.printers);
      elements.origins.value = state.settings.allowedOrigins.join("\n");
      elements.startup.checked = state.startup;
      elements.startup.disabled = !state.serviceUrl;
      elements.version.textContent = `Versión ${state.version} · ${state.serviceUrl}`;
      elements.logPath.textContent = `Archivo persistente: ${state.logPath}`;
      renderLogs(state.logs);
    } catch (error) {
      message(error.message, true);
    }
  }

  elements.refresh.addEventListener("click", async () => {
    elements.refresh.disabled = true;
    try {
      const printers = await window.ikiway.refreshPrinters();
      renderPrinters(printers);
      const state = await window.ikiway.getStatus();
      renderStatus(state);
      message("Lista de impresoras actualizada.");
    } catch (error) { message(error.message, true); }
    finally { elements.refresh.disabled = false; }
  });

  elements.printers.addEventListener("change", async () => {
    if (!elements.printers.value) return;
    elements.printers.disabled = true;
    try {
      const state = await window.ikiway.selectPrinter(elements.printers.value);
      renderStatus(state);
      renderPrinters(state.printers);
      message("Impresora seleccionada y guardada.");
    } catch (error) { message(error.message, true); }
    finally { elements.printers.disabled = false; }
  });

  elements.test.addEventListener("click", async () => {
    if (!window.confirm("Se imprimirá y cortará un comprobante de prueba. ¿Continuar?")) return;
    elements.test.disabled = true;
    message("Enviando prueba a la impresora…");
    try {
      await window.ikiway.testPrint();
      message("Prueba enviada. Verifica el papel impreso.");
    } catch (error) { message(error.message, true); }
    finally { elements.test.disabled = false; refreshLogs(); }
  });

  elements.saveOrigins.addEventListener("click", async () => {
    elements.saveOrigins.disabled = true;
    try {
      const settings = await window.ikiway.saveOrigins(elements.origins.value);
      elements.origins.value = settings.allowedOrigins.join("\n");
      message("Orígenes guardados. El cambio ya está activo.");
    } catch (error) { message(error.message, true); }
    finally { elements.saveOrigins.disabled = false; }
  });

  elements.startup.addEventListener("change", async () => {
    try {
      const result = await window.ikiway.setStartup(elements.startup.checked);
      elements.startup.checked = result.enabled;
      if (!result.available) message("El inicio automático se configura desde la versión instalada.", true);
      else message(result.enabled ? "El agente se iniciará con Windows." : "Inicio automático desactivado.");
    } catch (error) { message(error.message, true); }
  });

  elements.checkUpdates.addEventListener("click", async () => {
    elements.checkUpdates.disabled = true;
    try {
      renderUpdate(await window.ikiway.checkUpdates());
    } catch (error) { message(error.message, true); }
  });

  elements.installUpdate.addEventListener("click", async () => {
    if (!window.confirm("La aplicación se cerrará y reiniciará para instalar la actualización. ¿Continuar?")) return;
    try {
      const started = await window.ikiway.installUpdate();
      if (!started) message("La actualización todavía no está lista para instalar.", true);
    } catch (error) { message(error.message, true); }
  });

  elements.refreshLogs.addEventListener("click", async () => {
    elements.refreshLogs.disabled = true;
    await refreshLogs();
    elements.refreshLogs.disabled = false;
  });

  setInterval(async () => {
    try { renderStatus(await window.ikiway.getStatus()); } catch {}
  }, 5000);
  setInterval(refreshLogs, 3000);
  window.ikiway.onUpdateState(renderUpdate);
  load();
})();
