# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _, scrub
from frappe.utils import flt, nowdate, getdate, cstr, cint
from erpnext.stock.report.stock_ledger.stock_ledger import get_item_group_condition
from erpnext.stock.utils import has_valuation_read_permission
from erpnext.stock.doctype.item.item import convert_item_uom_for
from frappe.model.meta import get_field_precision
from frappe.desk.reportview import build_match_conditions
import json


def execute(filters=None):
	filters = frappe._dict(filters or {})
	filters.date = getdate(filters.date or nowdate())
	filters.from_date = filters.date
	filters.to_date = frappe.utils.add_days(filters.from_date, 4)

	if filters.buying_selling == "Selling":
		filters.standard_price_list = frappe.db.get_single_value("Selling Settings", "selling_price_list")
	elif filters.buying_selling == "Buying":
		filters.standard_price_list = frappe.db.get_single_value("Buying Settings", "buying_price_list")

	price_list_settings = frappe.get_single("Price List Settings")

	data, price_lists = get_data(filters)
	columns = get_columns(filters, price_lists)

	item_group_wise_data = {}
	for d in data:
		item_group_wise_data.setdefault(d.item_group, []).append(d)

	res = []

	for item_group in price_list_settings.item_group_order or []:
		if item_group.item_group in item_group_wise_data:
			res += sorted(item_group_wise_data[item_group.item_group], key=lambda d: d.item_code)
			del item_group_wise_data[item_group.item_group]

	for items in item_group_wise_data.values():
		res += sorted(items, key=lambda d: d.item_code)

	return columns, res


def get_data(filters):
	conditions = get_item_conditions(filters, for_item_dt=False)
	item_conditions = get_item_conditions(filters, for_item_dt=True)

	price_lists, selected_price_list = get_price_lists(filters)
	price_lists_cond = " and p.price_list in ({0})".format(", ".join([frappe.db.escape(d) for d in price_lists or ['']]))

	item_data = frappe.db.sql("""
		select item.name as item_code, item.item_name, item.item_group, item.stock_uom, item.sales_uom, item.alt_uom, item.alt_uom_size,
			item.hide_in_price_list
		from tabItem item
		where disabled != 1 {0}
	""".format(item_conditions), filters, as_dict=1)

	po_data = frappe.db.sql("""
		select
			item.item_code,
			sum(if(item.qty - item.received_qty < 0, 0, item.qty - item.received_qty) * item.conversion_factor) as po_qty,
			sum(if(item.qty - item.received_qty < 0, 0, item.qty - item.received_qty) * item.conversion_factor * item.base_net_rate) as po_lc_amount
		from `tabPurchase Order Item` item
		inner join `tabPurchase Order` po on po.name = item.parent
		where item.docstatus = 1 and po.status != 'Closed' {0}
		group by item.item_code
	""".format(conditions), filters, as_dict=1) # TODO add valuation rate in PO and use that

	bin_data = frappe.db.sql("""
		select
			bin.item_code,
			sum(bin.actual_qty) as actual_qty,
			sum(bin.stock_value) as stock_value
		from tabBin bin, tabItem item
		where item.name = bin.item_code {0}
		group by bin.item_code
	""".format(item_conditions), filters, as_dict=1)

	item_price_data = frappe.db.sql("""
		select p.name, p.price_list, p.item_code, p.price_list_rate, p.currency, p.uom,
			ifnull(p.valid_from, '2000-01-01') as valid_from
		from `tabItem Price` p
		inner join `tabItem` item on item.name = p.item_code
		where %(date)s between ifnull(p.valid_from, '2000-01-01') and ifnull(p.valid_upto, '2500-12-31')
			and ifnull(p.customer, '') = '' and ifnull(p.supplier, '') = '' {0} {1}
		order by p.uom
	""".format(item_conditions, price_lists_cond), filters, as_dict=1)

	previous_item_prices = frappe.db.sql("""
		select p.price_list, p.item_code, p.price_list_rate, ifnull(p.valid_from, '2000-01-01') as valid_from, p.uom
		from `tabItem Price` as p
		inner join `tabItem` item on item.name = p.item_code
		where p.valid_upto is not null and p.valid_upto < %(date)s {0} {1}
		order by p.valid_upto desc
	""".format(item_conditions, price_lists_cond), filters, as_dict=1)

	items_map = {}
	for d in item_data:
		d["disable_item_formatter"] = 1

		default_uom = d.purchase_uom if filters.buying_selling == "Buying" else d.sales_uom
		if filters.uom:
			d['uom'] = filters.uom
		elif filters.default_uom == "Stock UOM":
			d['uom'] = d.stock_uom
		elif filters.default_uom == "Contents UOM":
			d['uom'] = d.alt_uom or default_uom
		else:
			d['uom'] = default_uom

		if not d.get('uom'):
			d['uom'] = d.stock_uom

		d['print_in_price_list'] = cint(not d['hide_in_price_list'])
		del d['hide_in_price_list']

		d['alt_uom_size'] = convert_item_uom_for(d.alt_uom_size, d.item_code, d.uom, d.stock_uom)
		items_map[d.item_code] = d

	for d in po_data:
		if d.item_code in items_map:
			items_map[d.item_code].update(d)

	for d in bin_data:
		if d.item_code in items_map:
			items_map[d.item_code].update(d)

	for item_prices in [item_price_data, previous_item_prices]:
		for d in item_prices:
			if d.item_code in items_map:
				d.price_list_rate = convert_item_uom_for(d.price_list_rate, d.item_code, d.uom, items_map[d.item_code]['uom'],
					null_if_not_convertible=True, is_rate=True)

	item_price_map = {}
	for d in item_price_data:
		if d.item_code in items_map and d.price_list_rate is not None:
			current_item = items_map[d.item_code]
			price = item_price_map.setdefault(d.item_code, {}).setdefault(d.price_list, frappe._dict())
			pick_price = (cstr(d.uom) == cstr(current_item.uom)
					or (cstr(price.reference_uom) != cstr(current_item.uom) and cstr(d.uom) != current_item.stock_uom)
					or not price)

			if pick_price:
				price.current_price = d.price_list_rate
				price.valid_from = d.valid_from
				price.reference_uom = d.uom
				price.currency = d.currency

				if d.price_list == filters.standard_price_list:
					items_map[d.item_code].standard_rate = d.price_list_rate
					items_map[d.item_code].standard_rate_valid_from = d.valid_from

				show_amounts = has_valuation_read_permission()
				if show_amounts:
					price.item_price = d.name

	for d in previous_item_prices:
		if d.item_code in item_price_map and d.price_list in item_price_map[d.item_code] and d.price_list_rate is not None:
			price = item_price_map[d.item_code][d.price_list]
			if 'previous_price' not in price and d.valid_from < price.valid_from:
				price.previous_price = d.price_list_rate

	for item_code, d in items_map.items():
		d.actual_qty = convert_item_uom_for(flt(d.actual_qty), d.item_code, d.stock_uom, d.uom)
		d.po_qty = convert_item_uom_for(flt(d.po_qty), d.item_code, d.stock_uom, d.uom)

		d.po_lc_rate = flt(d.po_lc_amount) / d.po_qty if d.po_qty else 0
		d.valuation_rate = flt(d.stock_value) / d.actual_qty if d.actual_qty else 0

		d.balance_qty = d.actual_qty + d.po_qty
		d.avg_lc_rate = (flt(d.stock_value) + flt(d.po_lc_amount)) / d.balance_qty if d.balance_qty else 0
		d.margin_rate = (d.standard_rate - d.avg_lc_rate) * 100 / d.standard_rate if d.standard_rate else None

		for price_list, price in item_price_map.get(item_code, {}).items():
			d["rate_" + scrub(price_list)] = price.current_price
			d["currency_" + scrub(price_list)] = price.currency
			if d.standard_rate is not None:
				d["rate_diff_" + scrub(price_list)] = flt(price.current_price) - flt(d.standard_rate)
			if price.previous_price is not None:
				d["rate_old_" + scrub(price_list)] = price.previous_price
			if price.item_price:
				d["item_price_" + scrub(price_list)] = price.item_price
			if price.valid_from:
				d["valid_from_" + scrub(price_list)] = price.valid_from

		if filters.standard_price_list and filters.price_list_1:
			standard_rate = d.get("rate_" + scrub(filters.standard_price_list))
			comparison_rate = d.get("rate_" + scrub(filters.price_list_1))
			if standard_rate and comparison_rate:
				ratio_field = get_comparison_ratio_field(filters.standard_price_list, filters.price_list_1)
				d[ratio_field] = standard_rate / comparison_rate

		d['print_rate'] = d.get("rate_" + scrub(selected_price_list)) if selected_price_list else d.standard_rate

	if filters.filter_items_without_price:
		to_remove = []
		for item_code, d in items_map.items():
			if not d.get('print_rate'):
				to_remove.append(item_code)
		for item_code in to_remove:
			del items_map[item_code]

	return items_map.values(), price_lists


def get_price_lists(filters):
	def get_additional_price_lists():
		res = []
		for i in range(1):
			if filters.get('price_list_' + str(i+1)):
				res.append(filters.get('price_list_' + str(i+1)))
		return res

	def add_to_price_lists(price_list):
		if not price_list:
			return

		if not isinstance(price_list, list):
			price_list = [price_list]

		for pl in price_list:
			if pl and pl not in price_lists:
				price_lists.append(pl)

	conditions = []

	if filters.filter_price_list_by == "Disabled":
		conditions.append("enabled = 0")
	elif filters.filter_price_list_by == "Enabled":
		conditions.append("enabled = 1")

	if filters.buying_selling == "Selling":
		conditions.append("selling = 1")
	elif filters.buying_selling == "Buying":
		conditions.append("buying = 1")

	match_conditions = build_match_conditions("Price List")
	if match_conditions:
		conditions.append(match_conditions)

	conditions = "where " + " and ".join(conditions) if conditions else ""

	price_lists = []
	add_to_price_lists(filters.standard_price_list)

	if filters.customer:
		filters.selected_price_list = frappe.db.get_value("Customer", filters.customer, 'default_price_list')

	add_to_price_lists(filters.selected_price_list)

	additional_price_lists = get_additional_price_lists()
	add_to_price_lists(additional_price_lists)

	if not additional_price_lists and not filters.selected_price_list:
		add_to_price_lists(frappe.db.sql_list("""
			select name
			from `tabPrice List`
			{0}
			order by creation
		""".format(conditions)))

	return price_lists, filters.selected_price_list


def get_item_conditions(filters, for_item_dt):
	conditions = []

	if filters.get("item_code"):
		is_template = frappe.db.get_value("Item", filters.get('item_code'), 'has_variants')
		item_variant_of_field = "variant_of" if for_item_dt else "item_code"
		item_code_field = "name" if for_item_dt else "item_code"

		if is_template:
			conditions.append("(item.{0} = %(item_code)s or item.{1} = %(item_code)s)".format(
				item_code_field, item_variant_of_field))
		else:
			conditions.append("item.{0} = %(item_code)s".format(item_code_field))
	else:
		if filters.get("brand"):
			conditions.append("item.brand=%(brand)s")
		if filters.get("item_group"):
			conditions.append(get_item_group_condition(filters.get("item_group")))

		if filters.get("customer_provided_items") and for_item_dt:
			if filters.get("customer_provided_items") == "Customer Provided Items Only":
				conditions.append("item.is_customer_provided_item = 1")
			elif filters.get("customer_provided_items") == "Exclude Customer Provided Items":
				conditions.append("item.is_customer_provided_item = 0")

	if filters.get("supplier") and for_item_dt:
		if frappe.get_meta("Item").has_field("default_supplier"):
			conditions.append("item.default_supplier = %(supplier)s")

	return " and " + " and ".join(conditions) if conditions else ""


def get_columns(filters, price_lists):
	show_item_name = frappe.defaults.get_global_default('item_naming_by') != "Item Name"

	columns = [
		{"fieldname": "item_code", "label": _("Item Code"), "fieldtype": "Link", "options": "Item",
			"width": 100 if show_item_name else 200,
			"price_list_note": frappe.db.get_single_value("Price List Settings", "price_list_note")
		},
		{"fieldname": "item_name", "label": _("Item Name"), "fieldtype": "Data", "width": 150},
		{"fieldname": "print_in_price_list", "label": _("Print"), "fieldtype": "Check", "width": 50, "editable": 1},
		{"fieldname": "uom", "label": _("UOM"), "fieldtype": "Data", "width": 50},
		{"fieldname": "alt_uom_size", "label": _("Per Unit"), "fieldtype": "Float", "width": 68},
		# {"fieldname": "item_group", "label": _("Item Group"), "fieldtype": "Link", "options": "Item Group", "width": 120},
		{"fieldname": "po_qty", "label": _("PO Qty"), "fieldtype": "Float", "width": 80, "restricted": 1},
		{"fieldname": "po_lc_rate", "label": _("PO Rate"), "fieldtype": "Currency", "width": 90, "restricted": 1},
		{"fieldname": "actual_qty", "label": _("Stock Qty"), "fieldtype": "Float", "width": 80, "restricted": 1},
		{"fieldname": "valuation_rate", "label": _("Stock Rate"), "fieldtype": "Currency", "width": 90, "restricted": 1},
		{"fieldname": "avg_lc_rate", "label": _("Avg Rate"), "fieldtype": "Currency", "width": 90, "restricted": 1},
	]

	if filters.standard_price_list:
		if filters.show_valid_from:
			columns.append({
				"fieldname": "standard_rate_valid_from", "label": _("Valid From Std Rate"), "fieldtype": "Date", "width": 80
			})

		columns += [
			{"fieldname": "standard_rate", "label": filters.standard_price_list, "fieldtype": "Currency", "width": 110,
				"editable": 1, "price_list": filters.standard_price_list,
				"force_currency_symbol": 1, "options": "currency_" + scrub(filters.standard_price_list)},
			{"fieldname": "margin_rate", "label": _("Margin"), "fieldtype": "Percent", "width": 60, "restricted": 1},
		]

	for price_list in price_lists:
		if price_list != filters.standard_price_list:
			if filters.show_valid_from:
				columns.append({
					"fieldname": "valid_from_" + scrub(price_list), "label": _("Valid From {0}".format(price_list)), "fieldtype": "Date", "width": 80
				})

			columns.append({
				"fieldname": "rate_" + scrub(price_list),
				"label": price_list,
				"fieldtype": "Currency",
				"width": 110,
				"editable": 1,
				"price_list": price_list,
				"options": "currency_" + scrub(price_list),
				"force_currency_symbol": 1
			})

	if filters.standard_price_list and filters.price_list_1:
		columns.append({
			"fieldname": get_comparison_ratio_field(filters.standard_price_list, filters.price_list_1),
			"label": _("{0}/{1}").format(filters.standard_price_list, filters.price_list_1),
			"fieldtype": "Float",
			"width": 130,
		})

	show_amounts = has_valuation_read_permission()
	if not show_amounts:
		columns = list(filter(lambda d: not d.get('restricted'), columns))
		'''for c in columns:
			if c.get('editable'):
				del c['editable']'''

	if not show_item_name:
		columns = [c for c in columns if c.get('fieldname') != 'item_name']

	return columns


def get_comparison_ratio_field(numerator_price_list, divisor_price_list):
	return "ratio_" + scrub(numerator_price_list) + "_" + scrub(divisor_price_list)


@frappe.whitelist()
def set_multiple_item_pl_rate(effective_date, price_list, items):
	if isinstance(items, str):
		items = json.loads(items)

	for item in items:
		_set_item_pl_rate(effective_date, item.get('item_code'), price_list,
			item.get('price_list_rate'), item.get('uom'), item.get('conversion_factor'))


@frappe.whitelist()
def set_item_pl_rate(effective_date, item_code, price_list, price_list_rate, uom=None, conversion_factor=None, filters=None):
	effective_date = getdate(effective_date)
	_set_item_pl_rate(effective_date, item_code, price_list, price_list_rate, uom, conversion_factor)

	if filters is not None:
		if isinstance(filters, str):
			filters = json.loads(filters)

		filters['item_code'] = item_code
		return execute(filters)


def _set_item_pl_rate(effective_date, item_code, price_list, price_list_rate, uom=None, conversion_factor=None):
	from frappe.model.utils import get_fetch_values
	from erpnext.stock.get_item_details import get_item_price

	if not flt(price_list_rate):
		frappe.msgprint(_("Rate not set for Item {0} because rate is 0").format(item_code, price_list),
			alert=1, indicator="orange")
		return

	effective_date = getdate(effective_date)
	item_price_args = {
		"item_code": item_code,
		"price_list": price_list,
		"uom": uom,
		"min_qty": 0,
		"transaction_date": effective_date,
	}
	current_effective_item_price = get_item_price(item_price_args, item_code)

	existing_item_price = past_item_price = None
	if current_effective_item_price and getdate(current_effective_item_price.valid_from) == effective_date:
		existing_item_price = current_effective_item_price
	else:
		past_item_price = current_effective_item_price

	if current_effective_item_price:
		price_precision = get_field_precision(frappe.get_meta("Item Price").get_field('price_list_rate'))

		converted_rate = convert_item_uom_for(price_list_rate, item_code, uom, current_effective_item_price.uom,
			conversion_factor=conversion_factor, is_rate=True)
		if flt(converted_rate, price_precision) == flt(current_effective_item_price.price_list_rate, price_precision):
			frappe.msgprint(_("Rate not set for Item {0} because it is the same").format(item_code, price_list),
				alert=1, indicator="blue")
			return

	item_price_args['period'] = 'future'
	future_item_price = get_item_price(item_price_args, item_code)

	# Update or add item price
	if existing_item_price:
		doc = frappe.get_doc("Item Price", existing_item_price.name)
		converted_rate = convert_item_uom_for(price_list_rate, item_code, uom, doc.uom,
			conversion_factor=conversion_factor, is_rate=True)
		doc.price_list_rate = converted_rate
	else:
		doc = frappe.new_doc("Item Price")
		doc.item_code = item_code
		doc.price_list = price_list
		doc.uom = uom
		doc.price_list_rate = flt(price_list_rate)
		doc.update(get_fetch_values("Item Price", 'item_code', item_code))
		doc.update(get_fetch_values("Item Price", 'price_list', price_list))

	doc.valid_from = effective_date
	if future_item_price:
		doc.valid_upto = frappe.utils.add_days(future_item_price.valid_from, -1)
	doc.save()

	# Update previous item price
	before_effective_date = frappe.utils.add_days(effective_date, -1)
	if past_item_price and past_item_price.valid_upto != before_effective_date:
		frappe.set_value("Item Price", past_item_price.name, 'valid_upto', before_effective_date)

	frappe.msgprint(_("Price updated for Item {0} in Price List {1}").format(item_code, price_list),
		alert=1, indicator='green')
