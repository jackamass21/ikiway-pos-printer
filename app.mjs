import express from "express";
import cors from "cors";
import { createHash } from "node:crypto";
import { buildReceipt, PayloadError } from "./receipt.mjs";

export function createApp({ print, status, logEvent = () => {}, allowedOrigins = ["http://localhost:8000", "http://127.0.0.1:8000"], printerOptions = {} }) {
  const app = express();
  const allowed = () => new Set(typeof allowedOrigins === "function" ? allowedOrigins() : allowedOrigins);
  const emitLog = (level, event, message, meta = {}) => {
    try { Promise.resolve(logEvent({ level, event, message, meta })).catch(() => {}); } catch {}
  };
  const requestMeta = (req, id = req.get("X-Print-Job-Id")) => ({
    jobId: id,
    orderId: req.body?.order?.id,
    ticket: req.body?.ticket?.ticket_number,
    copies: req.body?.config?.print_copies,
  });
  app.use((req, res, next) => {
    if (req.headers.origin && !allowed().has(req.headers.origin)) {
      if (req.path === "/print") emitLog("warn", "print.rejected", "Solicitud rechazada: origen no autorizado", { origin: req.headers.origin });
      return res.status(403).json({ ok: false, error: "Origen no autorizado. Configura ALLOWED_ORIGINS en el agente." });
    }
    if (req.headers.origin && req.headers["access-control-request-private-network"] === "true") res.set("Access-Control-Allow-Private-Network", "true");
    next();
  });
  app.use(cors({ origin: (origin, cb) => cb(null, !origin || allowed().has(origin)), methods: ["GET", "POST", "OPTIONS"], allowedHeaders: ["Content-Type", "X-Print-Job-Id"] }));
  app.use(express.json({ limit: "1mb" }));
  const jobs = new Map();
  let queue = Promise.resolve();
  let pending = 0;
  app.get("/health", (_req, res, next) => {
    try { res.json({ ok: true, agent: "ikiway-pos-printer-agent", schema: "ikiway.receipt.v1", ...status(), pending }); }
    catch (error) { next(error); }
  });
  app.post("/print", async (req, res, next) => {
    const receivedId = req.get("X-Print-Job-Id");
    emitLog("info", "print.received", "Solicitud de impresión recibida", requestMeta(req, receivedId));
    try {
      if (!req.is("application/json")) {
        emitLog("warn", "print.rejected", "Solicitud rechazada: se requiere application/json", requestMeta(req, receivedId));
        return res.status(415).json({ ok: false, error: "Se requiere application/json" });
      }
      const id = receivedId;
      if (!id || !/^[a-zA-Z0-9_-]{16,100}$/.test(id)) throw new PayloadError("Falta X-Print-Job-Id valido");
      const digest = createHash("sha256").update(JSON.stringify(req.body)).digest("hex");
      const now = Date.now();
      for (const [key, job] of jobs) if (job.finishedAt && now - job.finishedAt > 86400000) jobs.delete(key);
      let job = jobs.get(id);
      if (job && job.digest !== digest) {
        emitLog("warn", "print.rejected", "Identificador repetido con otro comprobante", requestMeta(req, id));
        return res.status(409).json({ ok: false, error: "El identificador ya corresponde a otro comprobante" });
      }
      if (!job) {
        if (pending >= 20 || jobs.size >= 10000) {
          emitLog("error", "print.rejected", "Cola de impresión llena", requestMeta(req, id));
          return res.status(503).json({ ok: false, error: "Cola de impresion llena" });
        }
        const buffer = buildReceipt(req.body, printerOptions);
        const copies = req.body.config.print_copies;
        pending++;
        job = { digest, finishedAt: null };
        emitLog("info", "print.queued", "Comprobante agregado a la cola", requestMeta(req, id));
        job.result = queue.then(async () => {
          try {
            emitLog("info", "print.started", "Enviando comprobante a la impresora USB", requestMeta(req, id));
            await print(buffer, copies);
            emitLog("info", "print.success", "Impresión enviada correctamente", requestMeta(req, id));
            return { status: 200, body: { ok: true, job_id: id, copies, message: "Datos enviados a la impresora" } };
          } catch (error) {
            emitLog("error", "print.error", `Error USB: ${error.message}`, requestMeta(req, id));
            return { status: 502, body: { ok: false, job_id: id, error: "No se pudo completar el envio USB. Revisa el papel antes de reimprimir; el ticket puede haberse impreso parcialmente." } };
          } finally { pending--; job.finishedAt = Date.now(); }
        });
        queue = job.result.then(() => undefined);
        jobs.set(id, job);
      } else {
        emitLog("info", "print.duplicate", "Consulta repetida del mismo trabajo; no se duplicó la impresión", requestMeta(req, id));
      }
      const result = await job.result;
      res.status(result.status).json(result.body);
    } catch (error) {
      emitLog("error", "print.rejected", `Solicitud de impresión inválida: ${error.message}`, requestMeta(req, receivedId));
      next(error);
    }
  });
  app.use((error, req, res, _next) => {
    const statusCode = error instanceof PayloadError || error.type === "entity.parse.failed" ? 400 : error.status === 413 ? 413 : 500;
    if (req.path === "/print" && error.type === "entity.parse.failed") {
      emitLog("error", "print.rejected", "Solicitud rechazada: JSON inválido");
    }
    res.status(statusCode).json({ ok: false, error: statusCode === 500 ? "Error interno del agente" : error.message });
  });
  return app;
}
