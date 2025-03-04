# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

# ERPNext - web based ERP (http://erpnext.com)
# For license information, please see license.txt

import frappe

from frappe.utils import cstr, flt, getdate, new_line_sep, nowdate, ceil
from frappe import msgprint, _
from frappe.model.mapper import get_mapped_doc
from erpnext.stock.stock_balance import update_bin_qty, get_indented_qty
from erpnext.controllers.buying_controller import BuyingController
from erpnext.manufacturing.doctype.work_order.work_order import get_item_details
from erpnext.buying.utils import check_on_hold_or_closed_status, validate_for_items
from erpnext.stock.get_item_details import get_default_supplier
from six import string_types


form_grid_templates = {
	"items": "templates/form_grid/material_request_grid.html"
}


class MaterialRequest(BuyingController):
	def __init__(self, *args, **kwargs):
		super(MaterialRequest, self).__init__(*args, **kwargs)
		self.status_map = [
			["Draft", None],
			["Pending", "eval:self.per_ordered == 0 and self.docstatus == 1"],
			["Partially Ordered", "eval:self.per_ordered > 0 and self.docstatus == 1"],
			["Ordered", "eval:self.per_ordered == 100 and self.docstatus == 1"],
			["Partially Received", "eval:self.per_received > 0 and self.docstatus == 1"],
			["Received", "eval:self.per_received == 100 and self.docstatus == 1"],
			["Stopped", "eval:self.status == 'Stopped'"],
			["Cancelled", "eval:self.docstatus == 2"],
		]

	def get_feed(self):
		return _("{0}: {1}").format(self.status, self.material_request_type)

	def validate(self):
		super(MaterialRequest, self).validate()

		self.validate_schedule_date()
		self.check_for_on_hold_or_closed_status('Sales Order', 'sales_order')
		self.validate_uom_is_integer("uom", "qty")

		validate_for_items(self)
		self.set_alt_uom_qty()
		self.calculate_totals()

		self.set_completion_status()
		self.set_status()
		self.set_title()
		# self.validate_qty_against_so()
		# NOTE: Since Item BOM and FG quantities are combined, using current data, it cannot be validated
		# Though the creation of Material Request from a Production Plan can be rethought to fix this

	def on_submit(self):
		self.update_requested_qty()
		self.update_requested_qty_in_production_plan()
		if self.material_request_type == 'Purchase':
			self.validate_budget()

	def before_cancel(self):
		# if MRQ is already closed, no point saving the document
		check_on_hold_or_closed_status(self.doctype, self.name)

	def on_cancel(self):
		self.set_status(update=True, status='Cancelled')
		self.update_requested_qty()
		self.update_requested_qty_in_production_plan()

	def set_title(self):
		'''Set title as comma separated list of items'''
		if not self.title:
			items = ', '.join([d.item_name for d in self.items][:3])
			self.title = _('{0} Request for {1}').format(self.material_request_type, items)[:100]

	def set_completion_status(self, update=False, update_modified=True):
		data = self.get_completion_data()

		# update values in rows
		for d in self.items:
			d.ordered_qty = flt(data.ordered_qty_map.get(d.name))
			d.received_qty = flt(data.received_qty_map.get(d.name))

			if update:
				d.db_set({
					'ordered_qty': d.ordered_qty,
					'received_qty': d.received_qty,
				}, update_modified=update_modified)

		# update percentage in parent
		self.per_ordered = flt(self.calculate_status_percentage('ordered_qty', 'stock_qty', self.items))
		self.per_received = flt(self.calculate_status_percentage('received_qty', 'stock_qty', self.items))

		if update:
			self.db_set({
				'per_ordered': self.per_ordered,
				'per_received': self.per_received,
			}, update_modified=update_modified)

	def get_completion_data(self):
		data = frappe._dict()
		data.ordered_qty_map = {}
		data.received_qty_map = {}

		if self.docstatus == 1:
			row_names = [d.name for d in self.items]

			if row_names:
				if self.material_request_type == "Purchase":
					data.ordered_qty_map = dict(frappe.db.sql("""
						select i.material_request_item, sum(i.stock_qty)
						from `tabPurchase Order Item` i
						inner join `tabPurchase Order` p on p.name = i.parent
						where p.docstatus = 1 and i.material_request_item in %s
						group by i.material_request_item
					""", [row_names]))

					data.received_qty_map = dict(frappe.db.sql("""
						select i.material_request_item, sum(i.stock_qty)
						from `tabPurchase Receipt Item` i
						inner join `tabPurchase Receipt` p on p.name = i.parent
						where p.docstatus = 1 and i.material_request_item in %s
						group by i.material_request_item
					""", [row_names]))

				elif self.material_request_type in ("Material Issue", "Material Transfer", "Customer Provided"):
					data.ordered_qty_map = dict(frappe.db.sql("""
						select i.material_request_item, sum(i.stock_qty)
						from `tabStock Entry Detail` i
						inner join `tabStock Entry` p on p.name = i.parent
						where p.docstatus = 1 and i.material_request_item in %s
						group by i.material_request_item
					""", [row_names]))

					data.received_qty_map = data.ordered_qty_map

				elif self.material_request_type == "Manufacture":
					data.ordered_qty_map = dict(frappe.db.sql("""
						select p.material_request_item, sum(p.qty)
						from `tabWork Order` p
						where p.docstatus = 1 and p.material_request_item in %s
						group by p.material_request_item
					""", [row_names]))

		return data

	def validate_ordered_qty(self, from_doctype=None, row_names=None):
		self.validate_completed_qty('ordered_qty', 'stock_qty', self.items,
			allowance_type=None, from_doctype=from_doctype, row_names=row_names)

	def check_modified_date(self):
		mod_db = frappe.db.sql("""select modified from `tabMaterial Request` where name = %s""",
			self.name)
		date_diff = frappe.db.sql("""select TIMEDIFF('%s', '%s')"""
			% (mod_db[0][0], cstr(self.modified)))

		if date_diff and date_diff[0][0]:
			frappe.throw(_("{0} {1} has been modified. Please refresh.").format(_(self.doctype), self.name))

	def update_status(self, status):
		self.check_modified_date()
		self.status_can_change(status)
		self.set_status(update=True, status=status)
		self.update_requested_qty()

	def status_can_change(self, status):
		"""
		validates that `status` is acceptable for the present controller status
		and throws an Exception if otherwise.
		"""
		if self.status and self.status == 'Cancelled':
			# cancelled documents cannot change
			if status != self.status:
				frappe.throw(
					_("{0} {1} is cancelled so the action cannot be completed").
						format(_(self.doctype), self.name),
					frappe.InvalidStatusError
				)

		elif self.status and self.status == 'Draft':
			# draft document to pending only
			if status != 'Pending':
				frappe.throw(
					_("{0} {1} has not been submitted so the action cannot be completed").
						format(_(self.doctype), self.name),
					frappe.InvalidStatusError
				)

	def update_requested_qty(self, mr_item_rows=None):
		"""update requested qty (before ordered_qty is updated)"""
		no_partial_indent = frappe.get_cached_value("Stock Settings", None, "no_partial_indent")

		item_wh_list = []
		for d in self.get("items"):
			is_stock_item = frappe.db.get_value("Item", d.item_code, "is_stock_item", cache=1)
			if (not mr_item_rows or d.name in mr_item_rows or no_partial_indent)\
					and [d.item_code, d.warehouse] not in item_wh_list\
					and is_stock_item and d.warehouse:
				item_wh_list.append([d.item_code, d.warehouse])

		for item_code, warehouse in item_wh_list:
			update_bin_qty(item_code, warehouse, {
				"indented_qty": get_indented_qty(item_code, warehouse)
			})

	def update_requested_qty_in_production_plan(self):
		production_plans = []
		for d in self.get('items'):
			if d.production_plan and d.material_request_plan_item:
				qty = d.qty if self.docstatus == 1 else 0
				frappe.db.set_value('Material Request Plan Item',
					d.material_request_plan_item, 'requested_qty', qty)

				if d.production_plan not in production_plans:
					production_plans.append(d.production_plan)

		for production_plan in production_plans:
			doc = frappe.get_doc('Production Plan', production_plan)
			doc.set_status()
			doc.db_set('status', doc.status)

	def validate_qty_against_so(self):
		so_items = {} # Format --> {'SO/00001': {'Item/001': 120, 'Item/002': 24}}
		for d in self.get('items'):
			if d.sales_order:
				if not d.sales_order in so_items:
					so_items[d.sales_order] = {d.item_code: flt(d.qty)}
				else:
					if not d.item_code in so_items[d.sales_order]:
						so_items[d.sales_order][d.item_code] = flt(d.qty)
					else:
						so_items[d.sales_order][d.item_code] += flt(d.qty)

		for so_no in so_items.keys():
			for item in so_items[so_no].keys():
				already_indented = frappe.db.sql("""select sum(qty)
					from `tabMaterial Request Item`
					where item_code = %s and sales_order = %s and
					docstatus = 1 and parent != %s""", (item, so_no, self.name))
				already_indented = already_indented and flt(already_indented[0][0]) or 0

				actual_so_qty = frappe.db.sql("""select sum(stock_qty) from `tabSales Order Item`
					where parent = %s and item_code = %s and docstatus = 1""", (so_no, item))
				actual_so_qty = actual_so_qty and flt(actual_so_qty[0][0]) or 0

				if actual_so_qty and (flt(so_items[so_no][item]) + already_indented > actual_so_qty):
					frappe.throw(_("Material Request of maximum {0} can be made for Item {1} against Sales Order {2}").format(actual_so_qty - already_indented, item, so_no))

	def calculate_totals(self):
		self.total_qty = sum([flt(d.qty) for d in self.items])
		self.total_qty = flt(self.total_qty, self.precision("total_qty"))

		self.total_alt_uom_qty = sum([flt(d.alt_uom_qty) for d in self.items])
		self.total_alt_uom_qty = flt(self.total_alt_uom_qty, self.precision("total_alt_uom_qty"))

	@frappe.whitelist()
	def get_bom_items(self, bom, company, qty=1, fetch_exploded=1, warehouse=None):
		from erpnext.manufacturing.doctype.bom.bom import get_bom_items

		items = get_bom_items(bom, company, qty=qty, fetch_exploded=fetch_exploded)
		for d in items:
			d.warehouse = warehouse or self.set_warehouse or d.source_warehouse
			self.append('items', d)

		self.set_missing_item_details(for_validate=True)
		self.calculate_totals()

	@frappe.whitelist()
	def round_up_qty(self):
		for d in self.items:
			d.qty = ceil(flt(d.qty))

		self.set_missing_item_details(for_validate=True)
		self.calculate_totals()


def get_list_context(context=None):
	from erpnext.controllers.website_list_for_contact import get_list_context
	list_context = get_list_context(context)
	list_context.update({
		'show_sidebar': True,
		'show_search': True,
		'no_breadcrumbs': True,
		'title': _('Material Request'),
	})

	return list_context


def set_missing_values(source, target_doc):
	if target_doc.doctype == "Purchase Order" and getdate(target_doc.schedule_date) < getdate(nowdate()):
		target_doc.schedule_date = None
	target_doc.run_method("set_missing_values")
	target_doc.run_method("reset_taxes_and_charges")
	target_doc.run_method("calculate_taxes_and_totals")


def update_item(obj, target, source_parent, target_parent):
	target.conversion_factor = obj.conversion_factor
	target.qty = flt(flt(obj.stock_qty) - flt(obj.ordered_qty))/ target.conversion_factor
	target.stock_qty = (target.qty * target.conversion_factor)
	if getdate(target.schedule_date) < getdate(nowdate()):
		target.schedule_date = None


@frappe.whitelist()
def update_status(name, status):
	material_request = frappe.get_doc('Material Request', name)
	material_request.check_permission('write')
	material_request.run_method("update_status", status)


@frappe.whitelist()
def make_purchase_order(source_name, target_doc=None):
	def postprocess(source, target_doc):
		if frappe.flags.args and frappe.flags.args.default_supplier:
			target_doc.supplier = frappe.flags.args.default_supplier

			# items only for given default supplier
			supplier_items = []
			for d in target_doc.items:
				default_supplier = get_default_supplier(frappe.get_cached_doc("Item", d.item_code), {'company': target_doc.company})
				if frappe.flags.args.default_supplier == default_supplier:
					supplier_items.append(d)
			target_doc.items = supplier_items

		set_missing_values(source, target_doc)

	def item_condition(source, source_parent, target_parent):
		if source.name in [d.material_request_item for d in target_parent.get('items') if d.material_request_item]:
			return False

		return flt(source.ordered_qty) < flt(source.stock_qty)

	doclist = get_mapped_doc("Material Request", source_name, 	{
		"Material Request": {
			"doctype": "Purchase Order",
			"validation": {
				"docstatus": ["=", 1],
				"material_request_type": ["=", "Purchase"]
			},
			"field_map": [
				["schedule_date", "schedule_date"]
			]
		},
		"Material Request Item": {
			"doctype": "Purchase Order Item",
			"field_map": [
				["name", "material_request_item"],
				["parent", "material_request"],
				["stock_uom", "stock_uom"],
				["uom", "uom"],
				["sales_order", "sales_order"],
				["sales_order_item", "sales_order_item"],
				["schedule_date", "schedule_date"],
			],
			"postprocess": update_item,
			"condition": item_condition
		}
	}, target_doc, postprocess)

	return doclist


@frappe.whitelist()
def make_request_for_quotation(source_name, target_doc=None):
	doclist = get_mapped_doc("Material Request", source_name, 	{
		"Material Request": {
			"doctype": "Request for Quotation",
			"validation": {
				"docstatus": ["=", 1],
				"material_request_type": ["=", "Purchase"]
			}
		},
		"Material Request Item": {
			"doctype": "Request for Quotation Item",
			"field_map": [
				["name", "material_request_item"],
				["parent", "material_request"],
				["uom", "uom"]
			]
		}
	}, target_doc)

	return doclist


@frappe.whitelist()
def make_purchase_order_based_on_supplier(source_name, target_doc=None):
	if target_doc:
		if isinstance(target_doc, string_types):
			import json
			target_doc = frappe.get_doc(json.loads(target_doc))
		target_doc.set("items", [])

	material_requests, supplier_items = get_material_requests_based_on_supplier(source_name)

	def item_condition(source, source_parent, target_parent):
		if source.name in [d.material_request_item for d in target_parent.get('items') if d.material_request_item]:
			return False

		return flt(source.ordered_qty) < flt(source.stock_qty)

	def postprocess(source, target_doc):
		target_doc.supplier = source_name
		if getdate(target_doc.schedule_date) < getdate(nowdate()):
			target_doc.schedule_date = None
		target_doc.set("items", [d for d in target_doc.get("items")
			if d.get("item_code") in supplier_items and d.get("qty") > 0])

		set_missing_values(source, target_doc)

	for mr in material_requests:
		target_doc = get_mapped_doc("Material Request", mr, 	{
			"Material Request": {
				"doctype": "Purchase Order",
			},
			"Material Request Item": {
				"doctype": "Purchase Order Item",
				"field_map": [
					["name", "material_request_item"],
					["parent", "material_request"],
					["uom", "stock_uom"],
					["uom", "uom"]
				],
				"postprocess": update_item,
				"condition": item_condition,
			}
		}, target_doc, postprocess)

	return target_doc


def get_material_requests_based_on_supplier(supplier):
	supplier_items = [d.name for d in frappe.db.get_all("Item", {"default_supplier": supplier}, 'name')]
	if not supplier_items:
		frappe.throw(_("{0} is not the default supplier for any items.".format(supplier)))

	material_requests = frappe.db.sql_list("""select distinct mr.name
		from `tabMaterial Request` mr, `tabMaterial Request Item` mr_item
		where mr.name = mr_item.parent
			and mr_item.item_code in (%s)
			and mr.material_request_type = 'Purchase'
			and mr.per_ordered < 99.99
			and mr.docstatus = 1
			and mr.status != 'Stopped'
		order by mr_item.item_code""" % ', '.join(['%s']*len(supplier_items)),
		tuple(supplier_items))

	return material_requests, supplier_items


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_default_supplier_query(doctype, txt, searchfield, start, page_len, filters):
	doc = frappe.get_doc("Material Request", filters.get("doc"))

	suppliers = []
	for d in doc.items:
		default_supplier = get_default_supplier(d.item_code, doc)
		if default_supplier not in suppliers:
			suppliers.append(default_supplier)

	return [[d] for d in suppliers if d]


@frappe.whitelist()
def make_supplier_quotation(source_name, target_doc=None):
	def postprocess(source, target_doc):
		set_missing_values(source, target_doc)

	doclist = get_mapped_doc("Material Request", source_name, {
		"Material Request": {
			"doctype": "Supplier Quotation",
			"validation": {
				"docstatus": ["=", 1],
				"material_request_type": ["=", "Purchase"]
			}
		},
		"Material Request Item": {
			"doctype": "Supplier Quotation Item",
			"field_map": {
				"name": "material_request_item",
				"parent": "material_request",
				"sales_order": "sales_order"
			}
		}
	}, target_doc, postprocess)

	return doclist


@frappe.whitelist()
def make_stock_entry(source_name, target_doc=None):
	def item_condition(source, source_parent, target_parent):
		if source.name in [d.material_request_item for d in target_parent.get('items') if d.material_request_item]:
			return False

		return flt(source.ordered_qty) < flt(source.stock_qty)

	def update_item(obj, target, source_parent, target_parent):
		qty = flt(flt(obj.stock_qty) - flt(obj.ordered_qty))/ target.conversion_factor \
			if flt(obj.stock_qty) > flt(obj.ordered_qty) else 0
		target.qty = qty
		target.stock_qty = qty * obj.conversion_factor
		target.conversion_factor = obj.conversion_factor

		if source_parent.material_request_type == "Material Transfer" or source_parent.material_request_type == "Customer Provided":
			target.t_warehouse = obj.warehouse
		else:
			target.s_warehouse = obj.warehouse

		if source_parent.material_request_type == "Customer Provided":
			target.allow_zero_valuation_rate = 1

	def set_missing_values(source, target):
		target.purpose = source.material_request_type
		if source.job_card:
			target.purpose = 'Material Transfer for Manufacture'

		if source.material_request_type == "Customer Provided":
			target.purpose = "Material Receipt"

		target.run_method("set_missing_values")
		target.run_method("calculate_rate_and_amount")
		target.set_stock_entry_type()
		target.set_job_card_data()

	doclist = get_mapped_doc("Material Request", source_name, {
		"Material Request": {
			"doctype": "Stock Entry",
			"validation": {
				"docstatus": ["=", 1],
				"material_request_type": ["in", ["Material Transfer", "Material Issue", "Customer Provided"]]
			}
		},
		"Material Request Item": {
			"doctype": "Stock Entry Detail",
			"field_map": {
				"name": "material_request_item",
				"parent": "material_request",
				"stock_uom": "stock_uom",
				"uom": "uom",
			},
			"field_no_map": ['expense_account'],
			"postprocess": update_item,
			"condition": item_condition,
		}
	}, target_doc, set_missing_values)

	return doclist


@frappe.whitelist()
def raise_work_orders(material_request):
	mr= frappe.get_doc("Material Request", material_request)
	errors =[]
	work_orders = []
	default_wip_warehouse = frappe.db.get_single_value("Manufacturing Settings", "default_wip_warehouse")

	for d in mr.items:
		if (d.stock_qty - d.ordered_qty) > 0:
			if frappe.db.exists("BOM", {"item": d.item_code, "is_default": 1}):
				wo_order = frappe.new_doc("Work Order")
				wo_order.update({
					"production_item": d.item_code,
					"qty": d.stock_qty - d.ordered_qty,
					"fg_warehouse": d.warehouse,
					"wip_warehouse": default_wip_warehouse,
					"description": d.description,
					"stock_uom": d.stock_uom,
					"expected_delivery_date": d.schedule_date,
					"sales_order": d.sales_order,
					"bom_no": get_item_details(d.item_code).bom_no,
					"material_request": mr.name,
					"material_request_item": d.name,
					"planned_start_date": mr.transaction_date,
					"company": mr.company
				})

				wo_order.set_work_order_operations()
				wo_order.save()

				work_orders.append(wo_order.name)
			else:
				errors.append(_("Row {0}: Bill of Materials not found for the Item {1}").format(d.idx, d.item_code))

	if work_orders:
		message = [frappe.utils.get_link_to_form("Work Order", p, target="_blank") for p in work_orders]
		msgprint(_("The following Work Orders were created:") + '\n' + new_line_sep(message))

	if errors:
		frappe.throw(_("Work Order cannot be created for following reason:") + '\n' + new_line_sep(errors))

	return work_orders


@frappe.whitelist()
def create_pick_list(source_name, target_doc=None):
	doc = get_mapped_doc('Material Request', source_name, {
		'Material Request': {
			'doctype': 'Pick List',
			'field_map': {
				'material_request_type': 'purpose'
			},
			'validation': {
				'docstatus': ['=', 1]
			}
		},
		'Material Request Item': {
			'doctype': 'Pick List Item',
			'field_map': {
				'name': 'material_request_item',
				'qty': 'stock_qty'
			},
		},
	}, target_doc)

	doc.set_item_locations()

	return doc
