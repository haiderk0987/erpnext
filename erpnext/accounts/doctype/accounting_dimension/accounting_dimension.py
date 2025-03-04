# -*- coding: utf-8 -*-
# Copyright (c) 2019, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
import json
from frappe.model.document import Document
from frappe.custom.doctype.custom_field.custom_field import create_custom_field
from frappe import scrub
from frappe.utils import cstr
from frappe.utils.background_jobs import enqueue
from frappe.model import core_doctypes_list

class AccountingDimension(Document):
	def before_insert(self):
		self.set_fieldname_and_label()

	def validate(self):
		if self.document_type in core_doctypes_list + ('Accounting Dimension', 'Project',
				'Cost Center', 'Accounting Dimension Detail') :

			msg = _("Not allowed to create accounting dimension for {0}").format(self.document_type)
			frappe.throw(msg)

		exists = frappe.db.get_value("Accounting Dimension", {'document_type': self.document_type}, ['name'])

		if exists and self.is_new():
			frappe.throw("Document Type already used as a dimension")

	def after_insert(self):
		if frappe.flags.in_test:
			make_dimension_in_accounting_doctypes(doc=self)
		else:
			frappe.enqueue(make_dimension_in_accounting_doctypes, doc=self)

	def on_trash(self):
		if frappe.flags.in_test:
			delete_accounting_dimension(doc=self)
		else:
			frappe.enqueue(delete_accounting_dimension, doc=self)

	def set_fieldname_and_label(self):
		if not self.label:
			self.label = cstr(self.document_type)

		if not self.fieldname:
			self.fieldname = scrub(self.label)

def make_dimension_in_accounting_doctypes(doc):
	doclist = get_doctypes_with_dimensions()
	doc_count = len(get_accounting_dimensions(cache=False))
	count = 0

	for doctype in doclist:

		if (doc_count + 1) % 2 == 0:
			insert_after_field = 'dimension_col_break'
		else:
			insert_after_field = 'accounting_dimensions_section'

		df = {
			"fieldname": doc.fieldname,
			"label": doc.label,
			"fieldtype": "Link",
			"options": doc.document_type,
			"insert_after": insert_after_field,
			"ignore_user_permissions": 1,
			"owner": "Administrator"
		}

		if doctype == "Budget":
			add_dimension_to_budget_doctype(df, doc)
		else:
			create_custom_field(doctype, df)

		count += 1

		frappe.publish_progress(count*100/len(doclist), title = _("Creating Dimensions..."))
		frappe.clear_cache(doctype=doctype)

def add_dimension_to_budget_doctype(df, doc):
	df.update({
		"insert_after": "cost_center",
		"depends_on": "eval:doc.budget_against == '{0}'".format(doc.document_type)
	})

	create_custom_field("Budget", df)

	property_setter = frappe.db.exists("Property Setter", "Budget-budget_against-options")

	if property_setter:
		property_setter_doc = frappe.get_doc("Property Setter", "Budget-budget_against-options")
		property_setter_doc.value = property_setter_doc.value + "\n" + doc.document_type
		property_setter_doc.save()

		frappe.clear_cache(doctype='Budget')
	else:
		frappe.get_doc({
			"doctype": "Property Setter",
			"doctype_or_field": "DocField",
			"doc_type": "Budget",
			"field_name": "budget_against",
			"property": "options",
			"property_type": "Text",
			"value": "\nCost Center\nProject\n" + doc.document_type
		}).insert(ignore_permissions=True)


def delete_accounting_dimension(doc):
	doclist = get_doctypes_with_dimensions()

	frappe.db.sql("""
		DELETE FROM `tabCustom Field`
		WHERE fieldname = %s
		AND dt IN (%s)""" %			#nosec
		('%s', ', '.join(['%s']* len(doclist))), tuple([doc.fieldname] + doclist))

	frappe.db.sql("""
		DELETE FROM `tabProperty Setter`
		WHERE field_name = %s
		AND doc_type IN (%s)""" %		#nosec
		('%s', ', '.join(['%s']* len(doclist))), tuple([doc.fieldname] + doclist))

	budget_against_property = frappe.get_doc("Property Setter", "Budget-budget_against-options")
	value_list = budget_against_property.value.split('\n')[3:]

	if doc.document_type in value_list:
		value_list.remove(doc.document_type)

	budget_against_property.value = "\nCost Center\nProject\n" + "\n".join(value_list)
	budget_against_property.save()

	for doctype in doclist:
		frappe.clear_cache(doctype=doctype)

@frappe.whitelist()
def disable_dimension(doc):
	if frappe.flags.in_test:
		toggle_disabling(doc=doc)
	else:
		frappe.enqueue(toggle_disabling, doc=doc)

def toggle_disabling(doc):
	doc = json.loads(doc)

	if doc.get('disabled'):
		df = {"read_only": 1}
	else:
		df = {"read_only": 0}

	doclist = get_doctypes_with_dimensions()

	for doctype in doclist:
		field = frappe.db.get_value("Custom Field", {"dt": doctype, "fieldname": doc.get('fieldname')})
		if field:
			custom_field = frappe.get_doc("Custom Field", field)
			custom_field.update(df)
			custom_field.save()

		frappe.clear_cache(doctype=doctype)

def get_doctypes_with_dimensions():
	doclist = [
		"GL Entry",

		"Sales Invoice", "Sales Invoice Item", "POS Profile",
		"Purchase Invoice", "Purchase Invoice Item",
		"Opening Invoice Creation Tool", "Opening Invoice Creation Tool Item",

		"Delivery Note", "Delivery Note Item",
		"Purchase Receipt", "Purchase Receipt Item",
		"Sales Taxes and Charges", "Purchase Taxes and Charges",

		"Journal Entry", "Journal Entry Account",
		"Expense Entry", "Expense Entry Detail",
		"Payment Entry", "Payment Entry Deduction",

		"Stock Entry", "Stock Entry Detail",
		"Packing Slip", "Packing Slip Packaging Material",
		"Stock Reconciliation", "Stock Reconciliation Item",
		"Landed Cost Voucher", "Landed Cost Taxes and Charges",

		"Expense Claim", "Expense Claim Detail", "Expense Taxes and Charges",
		"Loyalty Program", "Shipping Rule",
		"Asset", "Asset Value Adjustment",
		"Fees", "Fee Schedule", "Fee Structure",
		"Budget",
		"Payroll Entry",
		"Travel Request",
		"Subscription", "Subscription Plan"
	]

	return doclist


def get_accounting_dimensions(as_list=True, cache=True):
	accounting_dimensions = _get_accounting_dimensions(cache=cache)

	if as_list:
		return [d.fieldname for d in accounting_dimensions]
	else:
		return accounting_dimensions


def _get_accounting_dimensions(cache):
	def generator():
		return frappe.get_all("Accounting Dimension", fields=["label", "fieldname", "disabled", "document_type"])

	return frappe.local_cache("accounting_dimensions", "accounting_dimensions", generator)


def get_checks_for_pl_and_bs_accounts():
	dimensions = frappe.db.sql("""SELECT p.name, p.label, p.disabled, p.fieldname, c.default_dimension, c.company, c.mandatory_for_pl, c.mandatory_for_bs
		FROM `tabAccounting Dimension`p ,`tabAccounting Dimension Detail` c
		WHERE p.name = c.parent""", as_dict=1)

	return dimensions

def get_dimension_with_children(doctype, dimension):

	if isinstance(dimension, list):
		dimension = dimension[0]

	all_dimensions = []
	lft, rgt = frappe.db.get_value(doctype, dimension, ["lft", "rgt"])
	children = frappe.get_all(doctype, filters={"lft": [">=", lft], "rgt": ["<=", rgt]}, order_by="lft")
	all_dimensions += [c.name for c in children]

	return all_dimensions

@frappe.whitelist()
def get_dimension_filters():
	dimension_filters = frappe.db.sql("""
		SELECT label, fieldname, document_type
		FROM `tabAccounting Dimension`
		WHERE disabled = 0
	""", as_dict=1)

	default_dimensions = frappe.db.sql("""SELECT p.fieldname, c.company, c.default_dimension
		FROM `tabAccounting Dimension Detail` c, `tabAccounting Dimension` p
		WHERE c.parent = p.name""", as_dict=1)

	default_dimensions_map = {}
	for dimension in default_dimensions:
		default_dimensions_map.setdefault(dimension.company, {})
		default_dimensions_map[dimension.company][dimension.fieldname] = dimension.default_dimension

	return dimension_filters, default_dimensions_map
