import unittest
import frappe
from frappe.contacts.address_and_contact import filter_dynamic_link_doctypes

class TestSearch(unittest.TestCase):
	#Search for the word "cond", part of the word "conduire" (Lead) in french.
	def test_contact_search_in_foreign_language(self):
		frappe.local.lang = 'fr'
		output = filter_dynamic_link_doctypes("DocType", "prospect", "name", 0, 20, {'fieldtype': 'HTML', 'fieldname': 'contact_html'})

		result = [['found' for x in y if x=="Lead"] for y in output]
		self.assertTrue(['found'] in result)

	def tearDown(self):
		frappe.local.lang = 'en'