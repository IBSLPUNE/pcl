from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def setup_custom_fields():
    create_custom_fields(
        {
            "Employee Checkin": [
                {
                    "fieldname": "etimeoffice_id",
                    "label": "E-timeoffice ID",
                    "fieldtype": "Data",
                    "insert_after": "device_id",
                    "read_only": 1,
                    "no_copy": 1,
                    "in_standard_filter": 1,
                }
            ]
        },
        update=True,
    )