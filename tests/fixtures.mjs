import { PNG } from "pngjs";

export function barcodeDataUri(width = 64, height = 18) {
  const png = new PNG({ width, height });
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const offset = (y * width + x) * 4;
      const color = x % 4 < 2 ? 0 : 255;
      png.data[offset] = color;
      png.data[offset + 1] = color;
      png.data[offset + 2] = color;
      png.data[offset + 3] = 255;
    }
  }
  return `data:image/png;base64,${PNG.sync.write(png).toString("base64")}`;
}

export function receipt(overrides = {}) {
  const base = {
    schema: "ikiway.receipt.v1",
    config: {
      paper_width: "80",
      print_copies: 1,
      ticket_header: "Gracias por preferirnos",
      ticket_footer: "Ikiway.cl",
      show_change_on_receipt: true,
    },
    order: {
      id: 42,
      branch_name: "Talcahuano",
      status: "paid",
      subtotal: "5034",
      tax_total: "956",
      total: "5990",
    },
    ticket: { ticket_number: "POS-000042", change_due: "4010" },
    lines: [{ description: "Cable USB-C edición ñ", quantity: "1", unit_price: "5990", line_total: "5990" }],
    payments: [{ method_label: "Efectivo", amount: "5990" }],
    electronic_document: {
      is_electronic: true,
      legal_name: "IKIWAY SPA",
      rut: "78.315.818-3",
      business_activity: "Venta al por menor",
      address: "Bulnes 136, Talcahuano",
      type_label: "Boleta electrónica afecta",
      folio: 321,
      issued_at: "08/09/2026 15:30",
      pdf417_data_uri: barcodeDataUri(),
      resolution_number: "80",
      resolution_date: "13/08/2026",
      is_certification: false,
      legal_footer: "",
    },
  };
  return { ...base, ...overrides };
}
