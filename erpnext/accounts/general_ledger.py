# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import frappe, erpnext
from frappe.utils import flt, cint
from frappe import _
from erpnext.accounts.utils import get_stock_and_account_balance
from frappe.model.meta import get_field_precision
from erpnext.accounts.doctype.budget.budget import validate_expense_against_budget
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_accounting_dimensions
from collections import OrderedDict


class ClosedAccountingPeriod(frappe.ValidationError): pass
class StockAccountInvalidTransaction(frappe.ValidationError): pass
class StockValueAndAccountBalanceOutOfSync(frappe.ValidationError): pass


def make_gl_entries(gl_map, cancel=False, adv_adj=False, merge_entries=True, update_outstanding='Yes', from_repost=False):
	if gl_map:
		if not cancel:
			validate_accounting_period(gl_map)
			gl_map = process_gl_map(gl_map, merge_entries)
			if gl_map and len(gl_map) > 1:
				save_entries(gl_map, adv_adj, update_outstanding, from_repost)
			elif gl_map:
				frappe.throw(_("Incorrect number of General Ledger Entries found. You might have selected a wrong Account in the transaction."))
		else:
			delete_gl_entries(gl_map, adv_adj=adv_adj, update_outstanding=update_outstanding)


def validate_accounting_period(gl_map):
	accounting_periods = frappe.db.sql(""" SELECT
			ap.name as name
		FROM
			`tabAccounting Period` ap, `tabClosed Document` cd
		WHERE
			ap.name = cd.parent
			AND ap.company = %(company)s
			AND cd.closed = 1
			AND cd.document_type = %(voucher_type)s
			AND %(date)s between ap.start_date and ap.end_date
			""", {
				'date': gl_map[0].posting_date,
				'company': gl_map[0].company,
				'voucher_type': gl_map[0].voucher_type
			}, as_dict=1)

	if accounting_periods:
		frappe.throw(_("You cannot create or cancel any accounting entries within in the closed Accounting Period {0}")
			.format(frappe.bold(accounting_periods[0].name)), ClosedAccountingPeriod)


def process_gl_map(gl_map, merge_entries=True):
	if merge_entries:
		gl_map = merge_similar_entries(gl_map)

	for entry in gl_map:
		# swap debit, credit if negative entry
		if flt(entry.debit) < 0 and flt(entry.credit) < 0:
			entry.debit, entry.credit = -flt(entry.credit), -flt(entry.debit)
		if flt(entry.debit_in_account_currency) < 0 and flt(entry.credit_in_account_currency) < 0:
			entry.debit_in_account_currency, entry.credit_in_account_currency = -flt(entry.credit_in_account_currency), -flt(entry.debit_in_account_currency)

		# handle debit side negative
		if flt(entry.debit) < 0:
			entry.credit = flt(entry.credit) - flt(entry.debit)
			entry.debit = 0.0
		if flt(entry.debit_in_account_currency) < 0:
			entry.credit_in_account_currency = flt(entry.credit_in_account_currency) - flt(entry.debit_in_account_currency)
			entry.debit_in_account_currency = 0.0

		# handle credit side negative
		if flt(entry.credit) < 0:
			entry.debit = flt(entry.debit) - flt(entry.credit)
			entry.credit = 0.0
		if flt(entry.credit_in_account_currency) < 0:
			entry.debit_in_account_currency = flt(entry.debit_in_account_currency) - flt(entry.credit_in_account_currency)
			entry.credit_in_account_currency = 0.0

	return gl_map


def merge_similar_entries(gl_map):
	from erpnext.accounts.doctype.gl_entry.gl_entry import remove_dimensions_not_allowed_for_bs_account

	merged_gl_map = OrderedDict()
	accounting_dimensions = get_accounting_dimensions()
	account_head_key_fields = get_account_head_key_fields(accounting_dimensions)

	for entry in gl_map:
		remove_dimensions_not_allowed_for_bs_account(entry)

		# if there is already an entry in this account then just add it
		# to that entry
		gle_key = tuple(entry.get(f) or "" for f in account_head_key_fields)
		same_head = merged_gl_map.get(gle_key)
		if same_head:
			same_head.debit = flt(same_head.debit) + flt(entry.debit)
			same_head.debit_in_account_currency = flt(same_head.debit_in_account_currency) + flt(entry.debit_in_account_currency)
			same_head.credit = flt(same_head.credit) + flt(entry.credit)
			same_head.credit_in_account_currency = flt(same_head.credit_in_account_currency) + flt(entry.credit_in_account_currency)
		else:
			merged_gl_map[gle_key] = entry

	company = gl_map[0].company if gl_map else erpnext.get_default_company()
	company_currency = erpnext.get_company_currency(company)
	precision = get_field_precision(frappe.get_meta("GL Entry").get_field("debit"), company_currency)

	# filter zero debit and credit entries
	merged_gle_list = []
	for entry in merged_gl_map.values():
		# handle both debit and credit set
		if flt(entry.debit) and flt(entry.credit):
			entry.debit = flt(entry.debit) - flt(entry.credit)
			entry.credit = 0

			if entry.debit < entry.credit:
				entry.debit, entry.credit = entry.credit, entry.debit

		if flt(entry.debit_in_account_currency) and flt(entry.credit_in_account_currency):
			entry.debit_in_account_currency = flt(entry.debit_in_account_currency) - flt(entry.credit_in_account_currency)
			entry.credit_in_account_currency = 0.0

			if entry.debit_in_account_currency < entry.credit_in_account_currency:
				entry.debit_in_account_currency, entry.credit_in_account_currency = entry.credit_in_account_currency, entry.debit_in_account_currency

		if flt(entry.debit, precision) != 0 or flt(entry.credit, precision) != 0:
			merged_gle_list.append(entry)

	return merged_gle_list


def get_account_head_key_fields(dimensions=None):
	account_head_fieldnames = [
		'account', 'party_type', 'party',
		'against_voucher_type', 'against_voucher',
		'cost_center', 'project',
		'remarks', 'reference_no', 'reference_date'
	]

	account_head_key_fields = account_head_fieldnames
	if dimensions:
		account_head_key_fields += dimensions

	return account_head_key_fields


def save_entries(gl_map, adv_adj, update_outstanding, from_repost=False):
	if not from_repost:
		validate_cwip_accounts(gl_map)

	round_off_debit_credit(gl_map)

	reference_documents_for_update = set()
	for entry in gl_map:
		make_entry(entry, adv_adj, from_repost)

		# check against budget
		if not from_repost:
			validate_expense_against_budget(entry)

		if update_outstanding and not from_repost:
			add_to_reference_documents_for_update(reference_documents_for_update, entry)

	for voucher_type, voucher_no, account, party_type, party in reference_documents_for_update:
		update_voucher_on_gl_posting(voucher_type, voucher_no, account, party_type, party, on_cancel=False)


def make_entry(args, adv_adj, from_repost=False):
	gle = frappe.new_doc("GL Entry")
	gle.update(args)
	gle.flags.ignore_permissions = 1
	gle.flags.from_repost = from_repost
	gle.validate()
	gle.db_insert()
	gle.run_method("on_update_with_args", adv_adj, from_repost)
	gle.flags.ignore_validate = True
	gle.submit()


def validate_account_for_perpetual_inventory(gl_map):
	if cint(erpnext.is_perpetual_inventory_enabled(gl_map[0].company)):
		account_list = [gl_entries.account for gl_entries in gl_map]

		aii_accounts = [d.name for d in frappe.get_all("Account",
			filters={'account_type': 'Stock', 'is_group': 0, 'company': gl_map[0].company})]

		for account in account_list:
			if account not in aii_accounts:
				continue

			account_bal, stock_bal, warehouse_list = get_stock_and_account_balance(account,
				gl_map[0].posting_date, gl_map[0].company)

			if gl_map[0].voucher_type=="Journal Entry":
				# In case of Journal Entry, there are no corresponding SL entries,
				# hence deducting currency amount
				account_bal -= flt(gl_map[0].debit) - flt(gl_map[0].credit)
				if account_bal == stock_bal:
					frappe.throw(_("Account: {0} can only be updated via Stock Transactions")
						.format(account), StockAccountInvalidTransaction)

			# This has been comment for a temporary, will add this code again on release of immutable ledger
			# elif account_bal != stock_bal:
			# 	precision = get_field_precision(frappe.get_meta("GL Entry").get_field("debit"),
			# 		currency=frappe.get_cached_value('Company',  gl_map[0].company,  "default_currency"))

			# 	diff = flt(stock_bal - account_bal, precision)
			# 	error_reason = _("Stock Value ({0}) and Account Balance ({1}) are out of sync for account {2} and it's linked warehouses.").format(
			# 		stock_bal, account_bal, frappe.bold(account))
			# 	error_resolution = _("Please create adjustment Journal Entry for amount {0} ").format(frappe.bold(diff))
			# 	stock_adjustment_account = frappe.db.get_value("Company",gl_map[0].company,"stock_adjustment_account")

			# 	db_or_cr_warehouse_account =('credit_in_account_currency' if diff < 0 else 'debit_in_account_currency')
			# 	db_or_cr_stock_adjustment_account = ('debit_in_account_currency' if diff < 0 else 'credit_in_account_currency')

			# 	journal_entry_args = {
			# 	'accounts':[
			# 		{'account': account, db_or_cr_warehouse_account : abs(diff)},
			# 		{'account': stock_adjustment_account, db_or_cr_stock_adjustment_account : abs(diff) }]
			# 	}

			# 	frappe.msgprint(msg="""{0}<br></br>{1}<br></br>""".format(error_reason, error_resolution),
			# 		raise_exception=StockValueAndAccountBalanceOutOfSync,
			# 		title=_('Values Out Of Sync'),
			# 		primary_action={
			# 			'label': _('Make Journal Entry'),
			# 			'client_action': 'erpnext.route_to_adjustment_jv',
			# 			'args': journal_entry_args
			# 		})


def validate_cwip_accounts(gl_map):
	if gl_map[0].voucher_type != "Journal Entry":
		return

	cwip_enabled = any([cint(ac.enable_cwip_accounting) for ac in frappe.db.get_all("Asset Category","enable_cwip_accounting")])
	if cwip_enabled:
		cwip_accounts = [d[0] for d in frappe.db.sql("""select name from tabAccount
			where account_type = 'Capital Work in Progress' and is_group=0""")]

		for entry in gl_map:
			if entry.account in cwip_accounts:
				frappe.throw(
					_("Account: <b>{0}</b> is capital Work in progress and can not be updated by Journal Entry").format(entry.account))


def round_off_debit_credit(gl_map):
	precision = get_field_precision(frappe.get_meta("GL Entry").get_field("debit"),
		currency=frappe.get_cached_value('Company',  gl_map[0].company,  "default_currency"))

	debit_credit_diff = 0.0
	for entry in gl_map:
		entry.debit = flt(entry.debit, precision)
		entry.credit = flt(entry.credit, precision)
		debit_credit_diff += entry.debit - entry.credit

	debit_credit_diff = flt(debit_credit_diff, precision)

	if gl_map[0]["voucher_type"] in ("Journal Entry", "Payment Entry"):
		allowance = 5.0 / (10**precision)
	else:
		allowance = .5

	if abs(debit_credit_diff) >= allowance:
		frappe.throw(_("Debit and Credit not equal for {0} #{1}. Difference is {2}.")
			.format(gl_map[0].voucher_type, gl_map[0].voucher_no, debit_credit_diff))

	elif abs(debit_credit_diff) >= (1.0 / (10**precision)):
		make_round_off_gle(gl_map, debit_credit_diff, precision)


def make_round_off_gle(gl_map, debit_credit_diff, precision):
	round_off_account, round_off_cost_center = get_round_off_account_and_cost_center(gl_map[0].company)
	round_off_account_exists = False
	round_off_gle = frappe._dict()
	for d in gl_map:
		if d.account == round_off_account:
			round_off_gle = d
			debit_credit_diff -= flt(d.debit - d.credit)
			round_off_account_exists = True

	debit_credit_diff = flt(debit_credit_diff, precision)

	if round_off_account_exists and abs(debit_credit_diff) < (1.0 / (10**precision)):
		gl_map.remove(round_off_gle)
		return

	if not round_off_gle:
		for k in ["voucher_type", "voucher_no", "company",
			"posting_date", "remarks", "is_opening"]:
				round_off_gle[k] = gl_map[0][k]

	round_off_gle.update({
		"account": round_off_account,
		"debit_in_account_currency": abs(debit_credit_diff) if debit_credit_diff < 0 else 0,
		"credit_in_account_currency": debit_credit_diff if debit_credit_diff > 0 else 0,
		"debit": abs(debit_credit_diff) if debit_credit_diff < 0 else 0,
		"credit": debit_credit_diff if debit_credit_diff > 0 else 0,
		"cost_center": round_off_cost_center,
		"party_type": None,
		"party": None,
		"against_voucher_type": None,
		"against_voucher": None
	})

	if not round_off_account_exists:
		gl_map.append(round_off_gle)


def get_round_off_account_and_cost_center(company):
	round_off_account, round_off_cost_center = frappe.get_cached_value('Company',  company,
		["round_off_account", "round_off_cost_center"]) or [None, None]
	if not round_off_account:
		frappe.throw(_("Please mention Round Off Account in Company"))

	if not round_off_cost_center:
		frappe.throw(_("Please mention Round Off Cost Center in Company"))

	return round_off_account, round_off_cost_center


def delete_gl_entries(gl_entries=None, voucher_type=None, voucher_no=None, adv_adj=False, update_outstanding="Yes"):
	from erpnext.accounts.doctype.gl_entry.gl_entry import validate_balance_type, \
		check_freezing_date, validate_frozen_account

	if not gl_entries:
		gl_entries = frappe.db.sql("""
			select account, posting_date, party_type, party, cost_center, fiscal_year,voucher_type,
				voucher_no, against_voucher_type, against_voucher, cost_center, company
			from `tabGL Entry`
			where voucher_type=%s and voucher_no=%s
		""", (voucher_type, voucher_no), as_dict=True)

	if gl_entries:
		validate_accounting_period(gl_entries)
		check_freezing_date(gl_entries[0]["posting_date"], adv_adj)

	delete_voucher_gl_entries(voucher_type or gl_entries[0]["voucher_type"], voucher_no or gl_entries[0]["voucher_no"])

	reference_documents_for_update = set()
	for entry in gl_entries:
		validate_frozen_account(entry["account"], adv_adj)
		validate_balance_type(entry["account"], adv_adj)
		if not adv_adj:
			validate_expense_against_budget(entry)

		if update_outstanding and not adv_adj:
			add_to_reference_documents_for_update(reference_documents_for_update, entry)

	for voucher_type, voucher_no, account, party_type, party in reference_documents_for_update:
		update_voucher_on_gl_posting(voucher_type, voucher_no, account, party_type, party, on_cancel=True)


def delete_voucher_gl_entries(voucher_type, voucher_no):
	if voucher_type and voucher_no:
		frappe.db.sql("""
			delete from `tabGL Entry`
			where voucher_type = %s and voucher_no = %s
		""", (voucher_type, voucher_no))


def add_to_reference_documents_for_update(reference_documents_for_update, entry):
	if entry.get("against_voucher_type") and entry.get("against_voucher"):
		reference_documents_for_update.add((
			entry.get("against_voucher_type"),
			entry.get("against_voucher"),
			entry.get("account"),
			entry.get("party_type"),
			entry.get("party"),
		))
	# do not update self, let the controller handle it manually
	# elif entry.get("party_type") and entry.get("party"):
	# 	reference_documents_for_update.add((
	# 		entry.get("voucher_type"),
	# 		entry.get("voucher_no"),
	# 		entry.get("account"),
	# 		entry.get("party_type"),
	# 		entry.get("party"),
	# 	))

	if (
		entry.get("original_against_voucher_type")
		and entry.get("original_against_voucher")
		and entry.get("original_against_voucher_type") in ('Sales Order', 'Purchase Order', 'Employee Advance')
	):
		reference_documents_for_update.add((
			entry.get("original_against_voucher_type"),
			entry.get("original_against_voucher"),
			entry.get("account"),
			entry.get("party_type"),
			entry.get("party"),
		))


def update_voucher_on_gl_posting(voucher_type, voucher_no, account, party_type, party, on_cancel=False):
	from frappe.model.base_document import get_controller

	hook_methods = frappe.get_doc_hooks().get(voucher_type, {}).get("on_gl_against_voucher", [])
	controller_method = getattr(get_controller(voucher_type), "on_gl_against_voucher", None)
	if controller_method or hook_methods:
		reference_doc = frappe.get_doc(voucher_type, voucher_no)
		reference_doc.run_method("on_gl_against_voucher", account, party_type, party, on_cancel)
