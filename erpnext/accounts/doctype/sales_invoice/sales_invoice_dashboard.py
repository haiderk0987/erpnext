# import frappe
from frappe import _


def get_data():
	return {
		'fieldname': 'sales_invoice',
		'non_standard_fieldnames': {
			'Delivery Note': 'sales_invoice',
			'Journal Entry': 'reference_name',
			'Payment Entry': 'reference_name',
			'Payment Request': 'reference_name',
			'Sales Invoice': 'return_against',
			'Auto Repeat': 'reference_document',
		},
		'internal_links': {
			'Sales Order': ['items', 'sales_order'],
			'Delivery Note': ['items', 'delivery_note'],
			'Quotation': ['items', 'quotation'],
			'Packing Slip': ['items', 'packing_slip'],
		},
		'transactions': [
			{
				'label': _('Payment'),
				'items': ['Payment Entry', 'Journal Entry', 'Payment Request']
			},
			{
				'label': _('Previous Documents'),
				'items': ['Delivery Note', 'Sales Order', 'Quotation']
			},
			{
				'label': _('Reference'),
				'items': ['Packing Slip']
			},
			{
				'label': _('Returns'),
				'items': ['Sales Invoice']
			},
		]
	}
