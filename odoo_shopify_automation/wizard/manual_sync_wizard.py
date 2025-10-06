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
      ('export_product', 'Export Products to Shopify'),
      ('export_product_selective', 'Export Selected Products to Shopify'),
      ('export_order', 'Export Orders to Shopify'),
      ('export_customer', 'Export Customers to Shopify'),
  ],
                               string='Sync Type',
                               required=True)

  # For selective product export
  product_ids = fields.Many2many('product.product', string='Products to Export')
  export_all_products = fields.Boolean(
      'Export All Unmapped Products',
      default=False,
      help=
      'If checked, will create mappings and export all Odoo products that are not yet mapped to this Shopify instance'
  )

  @api.onchange('sync_type')
  def _onchange_sync_type(self):
    """Clear product selection when sync type changes"""
    if self.sync_type != 'export_product_selective':
      self.product_ids = False
      self.export_all_products = False

  def action_manual_sync(self): # pylint: disable=too-many-branches
    if not self.instance_id or not self.sync_type:
      raise UserError(_('Please select an instance and sync type.'))

    if self.sync_type == 'import_product':
      self.env['shopify.product'].import_products_from_shopify(self.instance_id)
    elif self.sync_type == 'import_order':
      self.env['shopify.order'].import_orders_from_shopify(self.instance_id)
    elif self.sync_type == 'import_customer':
      self.env['shopify.customer'].import_customers_from_shopify(self.instance_id)
    elif self.sync_type == 'export_product':
      # Export existing mappings that are pending or have errors
      products = self.env['shopify.product'].search([('instance_id', '=', self.instance_id.id),
                                                     ('sync_status', 'in', ['pending', 'error'])])
      if products:
        self.env['shopify.product'].export_products_to_shopify(self.instance_id, products)
      else:
        raise UserError(
            _('No pending products found to export. Use "Export Selected Products" to choose specific products.')
        )
    elif self.sync_type == 'export_product_selective':
      if self.export_all_products:
        # Export all unmapped products
        existing_mappings = self.env['shopify.product'].search([
            ('instance_id', '=', self.instance_id.id)
        ]).mapped('odoo_product_id')

        unmapped_products = self.env['product.product'].search([('id', 'not in',
                                                                 existing_mappings.ids),
                                                                ('active', '=', True)])

        if unmapped_products:
          for product in unmapped_products:
            self.env['shopify.product'].export_single_product_to_shopify(self.instance_id, product)
        else:
          raise UserError(_('No unmapped products found.'))
      elif self.product_ids:
        # Export selected products
        for product in self.product_ids:
          self.env['shopify.product'].export_single_product_to_shopify(self.instance_id, product)
      else:
        raise UserError(_('Please select products to export or check "Export All Unmapped Products".'))
    elif self.sync_type == 'export_order':
      orders = self.env['shopify.order'].search([('instance_id', '=', self.instance_id.id)])
      if hasattr(self.env['shopify.order'], 'export_orders_to_shopify'):
        self.env['shopify.order'].export_orders_to_shopify(self.instance_id, orders)
    elif self.sync_type == 'export_customer':
      customers = self.env['shopify.customer'].search([('instance_id', '=', self.instance_id.id)])
      if hasattr(self.env['shopify.customer'], 'export_customers_to_shopify'):
        self.env['shopify.customer'].export_customers_to_shopify(self.instance_id, customers)

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
