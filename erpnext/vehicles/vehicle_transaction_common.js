frappe.provide("erpnext.vehicles");

erpnext.vehicles.VehiclesController = erpnext.stock.StockController.extend({
	setup: function () {
		this.setup_posting_date_time_check();
	},

	onload: function() {
		this.setup_queries();
	},

	refresh: function () {
		erpnext.toggle_naming_series();
		erpnext.hide_company();
		this.set_dynamic_link();
		this.setup_route_options();
	},

	setup_queries: function () {
		var me = this;

		this.setup_warehouse_query();

		if (this.frm.fields_dict.customer) {
			this.frm.set_query('customer', erpnext.queries.customer);
		}
		if (this.frm.fields_dict.supplier) {
			this.frm.set_query('supplier', erpnext.queries.supplier);
		}

		if (this.frm.fields_dict.contact_person) {
			this.frm.set_query('contact_person', () => {
				return erpnext.queries.contact_query(me.frm.doc);
			});
		}

		if (this.frm.fields_dict.receiver_contact) {
			this.frm.set_query('receiver_contact', () => {
				return erpnext.queries.contact_query(me.frm.doc);
			});
		}

		if (this.frm.fields_dict.customer_address) {
			this.frm.set_query('customer_address', () => {
				return erpnext.queries.address_query(me.frm.doc);
			});
		}

		if (this.frm.fields_dict.item_code) {
			this.frm.set_query("item_code", function() {
				var filters = {"is_vehicle": 1};
				if (me.frm.doc.vehicle_booking_order) {
					filters.include_in_vehicle_booking = 1;
				}
				return erpnext.queries.item(filters);
			});
		}

		if (this.frm.fields_dict.vehicle_booking_order) {
			this.frm.set_query("vehicle_booking_order", function() {
				var filters = {"docstatus": 1};

				if (me.frm.doc.doctype === "Vehicle Receipt") {
					filters['delivery_status'] = 'To Receive';
				} else {
					filters['delivery_status'] = 'To Deliver';
				}

				return {
					filters: filters
				};
			});
		}
	},

	setup_route_options: function () {
		var vehicle_field = this.frm.get_docfield("vehicle");

		if (vehicle_field) {
			vehicle_field.get_route_options_for_new_doc = () => {
				return {
					"item_code": this.frm.doc.item_code,
					"item_name": this.frm.doc.item_name
				}
			};
		}
	},

	set_dynamic_link: function () {
		frappe.dynamic_link = {
			doc: this.frm.doc,
			fieldname: 'customer',
			doctype: 'Customer'
		};
	},

	vehicle_booking_order: function () {
		if (this.frm.doc.vehicle_booking_order) {
			this.get_vehicle_booking_order_details();
		} else {
			this.get_customer_details();
		}
	},

	customer: function () {
		this.get_customer_details();
	},

	get_customer_details: function () {
		var me = this;

		frappe.call({
			method: "erpnext.controllers.vehicle_stock_controller.get_customer_details",
			args: {
				args: {
					company: me.frm.doc.company,
					customer: me.frm.doc.customer,
					supplier: me.frm.doc.supplier,
					vehicle_booking_order: me.frm.doc.vehicle_booking_order,
					posting_date: me.frm.doc.posting_date
				}
			},
			callback: function (r) {
				if (r.message && !r.exc) {
					me.frm.set_value(r.message);
				}
			}
		});
	},

	get_vehicle_booking_order_details: function () {
		var me = this;
		if (me.frm.doc.vehicle_booking_order) {
			frappe.call({
				method: "erpnext.controllers.vehicle_stock_controller.get_vehicle_booking_order_details",
				args: {
					args: {
						company: me.frm.doc.company,
						customer: me.frm.doc.customer,
						supplier: me.frm.doc.supplier,
						vehicle_booking_order: me.frm.doc.vehicle_booking_order,
						posting_date: me.frm.doc.posting_date
					}
				},
				callback: function (r) {
					if (r.message && !r.exc) {
						me.frm.set_value(r.message);
					}
				}
			});
		}
	},

	contact_person: function () {
		this.get_contact_details(this.frm.doc.contact_person, "");
	},

	receiver_contact: function () {
		this.get_contact_details(this.frm.doc.receiver_contact, "receiver_");
	},

	get_contact_details: function (contact, prefix) {
		frappe.call({
			method: "erpnext.controllers.vehicle_stock_controller.get_contact_details",
			args: {
				contact: contact,
				prefix: prefix
			},
			callback: function (r) {
				if (r.message && !r.exc) {
					me.frm.set_value(r.message);
				}
			}
		});
	},
});

