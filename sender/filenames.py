#!/usr/bin/env python3
"""Plausible attachment file names.

A file called `attachment.pdf` tells the recipient the mail came from a
script.  These read like something a person actually attaches: an invoice, a
statement, a signed agreement, a payroll sheet.  One name is picked per mail,
with a random reference number / month so two mails never look cloned.

Only `pdf` and `xlsx` are listed on purpose: those are the two formats
`attachments.build_attachment()` really produces.  A name may never claim an
extension whose bytes we cannot generate, so anything else raises.
"""
from __future__ import annotations

import random

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# {n} reference number, {m} month, {y} year, {v} version, {d} day-month
PDF_TEMPLATES = (
    "Invoice_{n}",
    "Invoice-{n}-{m}",
    "Payment_Receipt_{n}",
    "Bank_Statement_{m}",
    "Account_Summary_{m}",
    "Signed_Agreement_{n}",
    "Contract_{n}_signed",
    "Quotation_{n}",
    "Delivery_Note_{n}",
    "Payslip_{m}",
    "Insurance_Policy_{n}",
    "Tax_Document_{y}",
    "Expense_Report_{m}",
    "Property_Inspection_{m}",
    "Travel_Itinerary_{m}",
    "Medical_Report_{n}",
    "Meeting_Minutes_{d}",
    "Purchase_Order_{n}",
    "Service_Agreement_{n}",
    "Flight_Ticket_{n}",
)

XLSX_TEMPLATES = (
    "Budget_{y}_v{v}",
    "Sales_Report_{m}",
    "Expense_Sheet_{m}",
    "Payroll_Summary_{m}",
    "Inventory_{m}",
    "Stock_Count_{m}",
    "Client_Contacts_{y}",
    "Project_Plan_{m}",
    "Timesheet_{m}",
    "Price_List_{y}",
    "Cash_Flow_{m}",
    "Attendance_{m}",
    "Mileage_Claim_{m}",
    "Sales_Forecast_{y}",
    "Vendor_List_{y}",
)

_EXTENSIONS = {"pdf": PDF_TEMPLATES, "xlsx": XLSX_TEMPLATES}


def attachment_name(ext: str) -> str:
    """Return a realistic file name for a format we can actually build."""
    ext = (ext or "pdf").lower().lstrip(".")
    templates = _EXTENSIONS.get(ext)
    if not templates:
        raise ValueError(f"no generator for attachment type {ext!r}")
    name = random.choice(templates).format(
        n=random.randint(1000, 99999),
        m=random.choice(MONTHS),
        y=random.choice((2025, 2026)),
        v=random.randint(2, 6),
        d="%02d-%s" % (random.randint(1, 28), random.choice(MONTHS)),
    )
    return f"{name}.{ext}"


if __name__ == "__main__":
    for _ in range(10):
        print(attachment_name("pdf"), "|", attachment_name("xlsx"))
