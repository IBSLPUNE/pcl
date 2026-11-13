frappe.ui.form.on("Etimeoffice Settings", {
    refresh(frm) {
        if (frm.is_new()) return;
        const group = __("E-timeoffice");

        frm.add_custom_button(__("Test Connection"), () => {
            frappe.call({
                method: "purbanchal_attendance.etimeoffice.sync.test_connection",
                freeze: true,
                freeze_message: __("Testing connection..."),
                callback: (r) => frappe.msgprint(JSON.stringify(r.message)),
            });
        }, group);

        frm.add_custom_button(__("Run Sync Now"), () => {
            if (frm.is_dirty()) {
                frappe.msgprint(__("Save the settings first."));
                return;
            }
            frappe.confirm(__("Run the sync now, using the current Dry Run setting?"), () => {
                frappe.call({
                    method: "purbanchal_attendance.etimeoffice.sync.run_sync_now",
                    callback: () => frappe.show_alert({
                        message: __("Sync queued. Check Etimeoffice Sync Log in a few minutes."),
                        indicator: "green",
                    }),
                });
            });
        }, group);

        frm.add_custom_button(__("Retry Unmapped"), () => {
            frappe.confirm(__("Import unmapped punches for employees who now have a device code?"), () => {
                frappe.call({
                    method: "purbanchal_attendance.etimeoffice.sync.retry_unmapped",
                    callback: () => frappe.show_alert({
                        message: __("Retry queued. Check Etimeoffice Punch in a few minutes."),
                        indicator: "green",
                    }),
                });
            });
        }, group);
    },
});