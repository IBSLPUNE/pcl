frappe.realtime.on("etoff_label_backfill", (data) => {
    frappe.show_alert({ message: data.message, indicator: data.indicator }, 15);
});

frappe.realtime.on("etoff_day_fallback", (data) => {
    frappe.show_alert({ message: data.message, indicator: data.indicator }, 15);
});

function start_label_backfill(dry_run) {
    frappe.call({
        method: "purbanchal_attendance.etimeoffice.inout_sync.start_label_backfill",
        args: { dry_run: dry_run },
        freeze: true,
        freeze_message: __("Queuing..."),
        callback: () => {
            frappe.show_alert({
                message: dry_run
                    ? __("Dry run started. The result will appear here in a minute or two.")
                    : __("Apply started. The result will appear here when it finishes."),
                indicator: "blue",
            }, 8);
        },
    });
}

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

        frm.add_custom_button(__("Preview IN/OUT Labels (Dry Run)"), () => {
            if (frm.is_dirty()) {
                frappe.msgprint(__("Save the settings first."));
                return;
            }
            start_label_backfill(1);
        }, group);

        frm.add_custom_button(__("Apply IN/OUT Labels to Old Check-ins"), () => {
            if (frm.is_dirty()) {
                frappe.msgprint(__("Save the settings first."));
                return;
            }
            frappe.confirm(
                __("This writes IN/OUT to every blank E-TIMEOFFICE check-in. Run the preview first. Continue?"),
                () => start_label_backfill(0)
            );
        }, group);

                frm.add_custom_button(__("Preview Day Fallback (Dry Run)"), () => {
            if (frm.is_dirty()) {
                frappe.msgprint(__("Save the settings first."));
                return;
            }
            frappe.call({
                method: "purbanchal_attendance.etimeoffice.inout_sync.start_day_fallback",
                args: { dry_run: 1 },
                freeze: true,
                freeze_message: __("Queuing..."),
                callback: () => frappe.show_alert({
                    message: __("Preview queued. Open Etimeoffice Sync Log and filter Employee Code Filter = IN/OUT DAY FALLBACK."),
                    indicator: "blue",
                }, 10),
            });
        }, group);

        frm.add_custom_button(__("Apply Day Fallback"), () => {
            if (frm.is_dirty()) {
                frappe.msgprint(__("Save the settings first."));
                return;
            }
            frappe.confirm(
                __("This writes IN and OUT to two-punch days confirmed by the vendor. Run the preview first. Continue?"),
                () => frappe.call({
                    method: "purbanchal_attendance.etimeoffice.inout_sync.start_day_fallback",
                    args: { dry_run: 0 },
                    freeze: true,
                    freeze_message: __("Queuing..."),
                    callback: () => frappe.show_alert({
                        message: __("Apply queued. The result is saved in Etimeoffice Sync Log."),
                        indicator: "blue",
                    }, 10),
                })
            );
        }, group);
                frm.add_custom_button(__("Clear Stale Sync Lock"), () => {
            frappe.confirm(
                __("Only use this if the scheduler is stuck and no sync is running. Continue?"),
                () => frappe.call({
                    method: "purbanchal_attendance.etimeoffice.sync.clear_sync_lock",
                    callback: () => {
                        frappe.show_alert({
                            message: __("Lock cleared. The next scheduled run will start."),
                            indicator: "green",
                        }, 8);
                        frm.reload_doc();
                    },
                })
            );
        }, group);
    },
});