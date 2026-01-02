from odoo import models, fields, api, _
from odoo.exceptions import UserError


class ShopifyManualSyncWizard(models.TransientModel):
  _name = 'shopify.manual.sync.wizard'
  _description = 'Shopify Manual Sync Wizard'

  instance_id = fields.Many2one('shopify.instance', string='Shopify Instance', required=True)
  sync_type = fields.Selection([
      ('import_product', 'Import Products from Shopify'),
      ('import_order', 'Import Orders from Shopify'),
      ('import_customer', 'Import Customers from Shopify'),
  ],
                               string='Sync Type',
                               required=True)

  product_ids = fields.Many2many('product.product', string='Products to Export')


  @api.onchange('sync_type')
  def _onchange_sync_type(self):
    """Clear product selection when sync type changes"""
    if self.sync_type != 'export_product_selective':
      self.product_ids = False

  def action_manual_sync(self): # pylint: disable=too-many-branches
    if not self.instance_id or not self.sync_type:
      raise UserError(_('Please select an instance and sync type.'))

    if self.sync_type == 'import_product':
      self.env['shopify.product'].import_products_from_shopify(self.instance_id)
    elif self.sync_type == 'import_order':
      self.env['shopify.order'].import_orders_from_shopify(self.instance_id)
    elif self.sync_type == 'import_customer':
      self.env['shopify.customer'].import_customers_from_shopify(self.instance_id)

    return {
        'type': 'ir.actions.client',
        'tag': 'display_notification',
        'params': {
            'title': 'Shopify Sync',
            'message': 'Sync operation completed successfully!',
            'type': 'success',
            'sticky': False,
        },
    }
