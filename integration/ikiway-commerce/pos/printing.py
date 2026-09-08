from django.utils import timezone

from sii.services.ted import render_pdf417_data_uri


def build_printer_payload(order, config, electronic_document):
    """Contrato del agente local; timbre a puntos enteros para cabezal de 203 dpi."""
    paper_width = config.receipt_paper_width if config else "80"
    if electronic_document["is_electronic"] and not electronic_document["ted_xml"]:
        raise ValueError("El DTE no tiene TED disponible para generar su PDF417.")
    doc = {key: value for key, value in electronic_document.items() if key not in {"document", "ted_xml", "issued_at", "pdf417_data_uri"}}
    issued_at = electronic_document["issued_at"]
    if timezone.is_aware(issued_at):
        issued_at = timezone.localtime(issued_at)
    doc["issued_at"] = issued_at.strftime("%d/%m/%Y %H:%M")
    try:
        doc["pdf417_data_uri"] = render_pdf417_data_uri(
            electronic_document["ted_xml"], columns=6 if paper_width == "58" else 10,
            security_level=5, scale=2, ratio=3, padding=8,
        )
    except (IndexError, ValueError) as exc:
        raise ValueError(
            "El TED no cabe en el PDF417 para este papel. Revisa el documento o utiliza papel de 80 mm."
        ) from exc
    ticket = order.pos_ticket
    return {
        "schema": "ikiway.receipt.v1",
        "config": {
            "paper_width": paper_width,
            "print_copies": config.print_copies if config else 1,
            "ticket_header": config.ticket_header if config else "",
            "ticket_footer": config.ticket_footer if config else "",
            "show_change_on_receipt": config.show_change_on_receipt if config else True,
        },
        "order": {
            "id": order.id, "branch_name": order.branch.name, "status": order.status,
            "subtotal": str(order.subtotal), "tax_total": str(order.tax_total), "total": str(order.total),
        },
        "ticket": {"ticket_number": ticket.ticket_number, "change_due": str(ticket.receipt_payload.get("change_due") or "0")},
        "lines": [
            {"description": line.description, "quantity": str(line.quantity), "unit_price": str(line.unit_price), "line_total": str(line.line_total)}
            for line in order.lines.all()
        ],
        "payments": [{"method_label": payment.get_method_display(), "amount": str(payment.amount)} for payment in order.payments.all()],
        "electronic_document": doc,
    }
