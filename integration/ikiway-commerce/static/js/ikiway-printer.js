(() => {
  "use strict";
  const script = document.currentScript;
  const payload = JSON.parse(document.getElementById("ikiway-printer-payload").textContent);
  const button = document.getElementById("agent-print");
  const reprint = document.getElementById("agent-reprint");
  const status = document.getElementById("agent-status");
  const agent = "http://127.0.0.1:17891";
  const storageKey = `ikiway-print:${location.pathname}`;
  // Misma pagina/recarga conserva ID: una respuesta perdida no duplica la venta.
  let jobId;
  try { jobId = sessionStorage.getItem(storageKey); } catch {}
  function newJob() {
    jobId = crypto.randomUUID();
    try { sessionStorage.setItem(storageKey, jobId); } catch {}
  }
  if (!jobId) newJob();
  let busy = false;
  async function print() {
    if (busy) return;
    busy = true;
    button.disabled = true;
    reprint.disabled = true;
    status.textContent = "Enviando comprobante al agente USB…";
    let submitted = false;
    try {
      const healthResponse = await fetch(`${agent}/health`, { signal: AbortSignal.timeout(5000) });
      if (!healthResponse.ok) throw new Error("Agente inaccesible. Revisa ALLOWED_ORIGINS y el permiso de red local del navegador.");
      const health = await healthResponse.json();
      if (health.schema !== payload.schema) throw new Error("Actualiza al agente USB Ikiway; el agente Tickeo no admite este comprobante.");
      if (!health.printer_connected) throw new Error("No hay una impresora USB seleccionada y conectada en el agente.");
      submitted = true;
      const response = await fetch(`${agent}/print`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Print-Job-Id": jobId },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(60000),
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || "No se completo el envio a la impresora.");
      status.textContent = `Comprobante enviado por USB (${result.copies} copia(s)). Verifica la salida de papel.`;
      button.textContent = "Consultar envio";
    } catch (error) {
      const network = error instanceof TypeError || error.name === "TimeoutError";
      status.textContent = network
        ? (submitted ? "No se pudo confirmar el envio. Revisa la impresora antes de reimprimir. Puedes consultar el envio con el mismo identificador." : "No se pudo conectar al agente. Inicialo en este equipo y revisa ALLOWED_ORIGINS y el permiso de red local del navegador.")
        : error.message;
      button.textContent = submitted ? "Consultar envio" : "Reintentar conexion";
    } finally {
      busy = false;
      button.disabled = false;
      reprint.disabled = false;
      if (submitted) reprint.hidden = false;
    }
  }
  button.addEventListener("click", print);
  reprint.addEventListener("click", () => {
    if (busy || !window.confirm("Esto enviara una nueva copia. Confirma que revisaste la salida de papel.")) return;
    newJob();
    print();
  });
  if (script.dataset.autoPrint === "true") window.addEventListener("load", print, { once: true });
})();
