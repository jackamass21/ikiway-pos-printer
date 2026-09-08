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

  async function load() {
    try {
      const state = await window.ikiway.getState();
      renderStatus(state);
      renderPrinters(state.printers);
      elements.origins.value = state.settings.allowedOrigins.join("\n");
      elements.startup.checked = state.startup;
      elements.startup.disabled = !state.serviceUrl;
      elements.version.textContent = `Versión ${state.version} · ${state.serviceUrl}`;
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
    finally { elements.test.disabled = false; }
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

  setInterval(async () => {
    try { renderStatus(await window.ikiway.getStatus()); } catch {}
  }, 5000);
  load();
})();
