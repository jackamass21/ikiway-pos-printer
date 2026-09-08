import { PNG } from "pngjs";
import iconv from "iconv-lite";

export class PayloadError extends Error {}
const fail = (message) => { throw new PayloadError(message); };
const clean = (value) => String(value ?? "").replace(/[\x00-\x1f\x7f-\x9f]/g, " ").normalize("NFC");
const money = (value) => `$${Math.round(Number(value)).toLocaleString("es-CL")}`;
const amount = (value, name) => {
  if (!["number", "string"].includes(typeof value) || String(value).trim() === "" || !Number.isFinite(Number(value))) fail(`${name}: monto invalido`);
};

export function validateReceipt(p) {
  if (!p || p.schema !== "ikiway.receipt.v1") fail("Se requiere schema ikiway.receipt.v1");
  if (!p.order || !p.ticket || !p.electronic_document || !p.config) fail("Faltan datos del comprobante");
  if (!p.order.id || !p.ticket.ticket_number) fail("Falta identificador de venta/ticket");
  if (!["58", "80"].includes(p.config.paper_width)) fail("Papel debe ser 58 u 80 mm");
  if (!Number.isInteger(p.config.print_copies) || p.config.print_copies < 1 || p.config.print_copies > 5) fail("Copias debe estar entre 1 y 5");
  if (!Array.isArray(p.lines) || !p.lines.length || p.lines.length > 500) fail("Se requieren entre 1 y 500 productos");
  if (!Array.isArray(p.payments) || p.payments.length > 20) fail("Pagos invalidos");
  for (const key of ["subtotal", "tax_total", "total"]) amount(p.order[key], key);
  for (const line of p.lines) {
    if (!line || !clean(line.description).trim()) fail("Producto sin descripcion");
    for (const key of ["quantity", "unit_price", "line_total"]) amount(line[key], key);
    if (Number(line.quantity) <= 0) fail("Cantidad invalida");
  }
  for (const payment of p.payments) {
    if (!payment || typeof payment.method_label !== "string") fail("Pago invalido");
    amount(payment.amount, "Pago");
  }
  if (p.ticket.change_due != null) amount(p.ticket.change_due, "Vuelto");
  const doc = p.electronic_document;
  if (typeof doc.is_electronic !== "boolean") fail("Tipo de comprobante invalido");
  if (doc.is_electronic && (!doc.pdf417_data_uri || !doc.folio)) fail("DTE sin folio o PDF417: no se imprimio");
  return p;
}

// GS v 0, escala 1:1. Nunca interpolar ni recortar un timbre PDF417.
export function pdf417Raster(uri, maxWidth) {
  if (typeof uri !== "string" || !/^data:image\/png;base64,[A-Za-z0-9+/]+={0,2}$/.test(uri) || uri.length > 400000) fail("PDF417 debe ser un PNG base64 valido");
  const buffer = Buffer.from(uri.split(",")[1], "base64");
  if (buffer.length < 24 || !buffer.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10]))) fail("PNG invalido");
  const width = buffer.readUInt32BE(16), height = buffer.readUInt32BE(20);
  if (!width || width > maxWidth || !height || height > 2048) fail(`PDF417 excede ${maxWidth} puntos o tiene dimensiones invalidas; regenerar desde el TED para este papel`);
  let png;
  try { png = PNG.sync.read(buffer, { checkCRC: true }); } catch { fail("PNG del PDF417 corrupto"); }
  const bytesPerRow = Math.ceil(width / 8);
  const raster = Buffer.alloc(bytesPerRow * height);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const i = (y * width + x) * 4;
      const luminance = (png.data[i] + png.data[i + 1] + png.data[i + 2]) / 3;
      const onWhite = (luminance * png.data[i + 3] + 255 * (255 - png.data[i + 3])) / 255;
      if (onWhite < 128) raster[y * bytesPerRow + (x >> 3)] |= 0x80 >> (x & 7);
    }
  }
  return Buffer.concat([Buffer.from([0x1d, 0x76, 0x30, 0, bytesPerRow & 255, bytesPerRow >> 8, height & 255, height >> 8]), raster]);
}

export function buildReceipt(payload, { encoding = "cp850", codePage = 2 } = {}) {
  const p = validateReceipt(payload);
  if (!iconv.encodingExists(encoding)) throw new Error("PRINTER_ENCODING no reconocido");
  if (!Number.isInteger(codePage) || codePage < 0 || codePage > 255) throw new Error("PRINTER_CODE_PAGE invalido");
  const columns = p.config.paper_width === "58" ? 32 : 42;
  const doc = p.electronic_document;
  // Validar y decodificar toda la imagen antes de abrir USB o imprimir texto.
  const barcode = doc.pdf417_data_uri ? pdf417Raster(doc.pdf417_data_uri, p.config.paper_width === "58" ? 384 : 512) : null;
  const parts = [Buffer.from([0x1b, 0x40, 0x1b, 0x74, codePage])];
  const command = (...bytes) => parts.push(Buffer.from(bytes));
  const text = (value) => {
    const chars = Array.from(clean(value));
    if (!chars.length) parts.push(Buffer.from("\n"));
    for (let i = 0; i < chars.length; i += columns) parts.push(iconv.encode(chars.slice(i, i + columns).join("") + "\n", encoding));
  };
  const optional = (value) => { if (value) text(value); };
  const row = (label, value) => {
    const left = clean(label), right = clean(value);
    if (left.length + right.length + 1 > columns) { text(left); text(right.padStart(columns)); }
    else text(left + " ".repeat(columns - left.length - right.length) + right);
  };
  const divider = () => text("-".repeat(columns));
  command(0x1b, 0x61, 1, 0x1b, 0x45, 1);
  optional(p.config.ticket_header);
  text(doc.legal_name || "IKIWAY");
  command(0x1b, 0x45, 0);
  optional(doc.rut && `RUT: ${doc.rut}`);
  optional(doc.business_activity && `Giro: ${doc.business_activity}`);
  optional(doc.address);
  optional(p.order.branch_name && `Sucursal: ${p.order.branch_name}`);
  divider();
  command(0x1b, 0x45, 1);
  text(doc.type_label || "Comprobante de venta");
  text(`Folio ${doc.folio || p.ticket.ticket_number}`);
  command(0x1b, 0x45, 0);
  optional(doc.issued_at);
  text(`Ticket ${p.ticket.ticket_number}`);
  divider();
  command(0x1b, 0x61, 0);
  for (const line of p.lines) {
    text(line.description);
    row(`${Number(line.quantity).toLocaleString("es-CL", { maximumFractionDigits: 3 })} x ${money(line.unit_price)}`, money(line.line_total));
  }
  divider();
  row("Neto/Bruto", money(p.order.subtotal));
  row("IVA incluido", money(p.order.tax_total));
  command(0x1b, 0x45, 1);
  row("TOTAL", money(p.order.total));
  command(0x1b, 0x45, 0);
  divider();
  for (const payment of p.payments) row(payment.method_label, money(payment.amount));
  if (p.config.show_change_on_receipt && Number(p.ticket.change_due)) row("Vuelto", money(p.ticket.change_due));
  divider();
  command(0x1b, 0x61, 1);
  if (p.order.status === "cancelled") text("VENTA ANULADA");
  optional(p.config.ticket_footer);
  if (barcode) {
    parts.push(barcode);
    text("Timbre electronico S.I.I.");
    if (doc.resolution_number || doc.resolution_date) text(`Resolucion ${doc.resolution_number || "-"} de ${doc.resolution_date || "-"}`);
    text("Verifique documento en www.sii.cl");
    if (doc.is_certification) text("Ambiente de certificacion");
    optional(doc.legal_footer);
  } else {
    text("Documento interno POS");
    text("DTE pendiente de emision");
  }
  command(0x1b, 0x64, 4, 0x1d, 0x56, 1);
  return Buffer.concat(parts);
}
