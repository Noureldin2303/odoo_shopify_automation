from odoo import models, fields, api
from datetime import datetime


class ShopifyCron(models.Model):
  _name = 'shopify.cron'
  _description = 'Shopify Scheduled Sync Cron'

  name = fields.Char('Cron Name', required=True)
  cron_type = fields.Selection([
      ('import_product', 'Import Products'),
      ('import_order', 'Import Orders'),
      ('import_customer', 'Import Customers'),
  ],
                               string='Cron Type',
                               required=True)
  instance_id = fields.Many2one('shopify.instance', string='Shopify Instance', required=True)
  active = fields.Boolean('Active', default=True)
  last_run = fields.Datetime('Last Run')
  note = fields.Text('Notes')

  def run_cron(self):
    for cron in self:
      if cron.cron_type == 'import_product':
        self.env['shopify.product'].import_products_from_shopify(cron.instance_id)
      elif cron.cron_type == 'import_order':
        self.env['shopify.order'].import_orders_from_shopify(cron.instance_id)
      elif cron.cron_type == 'import_customer':
        self.env['shopify.customer'].import_customers_from_shopify(cron.instance_id)

      cron.last_run = fields.Datetime.now()
