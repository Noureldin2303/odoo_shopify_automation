from odoo import models, fields, api
import logging
import requests
import json

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
  _inherit = 'sale.order'

  shopify_sync_enabled = fields.Boolean(
      'Enable Shopify Sync',
      default=False,
      help='If enabled, this order will be synced to/from Shopify instances')

  shopify_order_source = fields.Selection(
      [
          ('odoo', 'Created in Odoo'),
          ('shopify', 'Imported from Shopify'),
      ],
      string='Order Source',
      default='odoo',
      help='Indicates whether this order was created in Odoo or imported from Shopify')

  shopify_last_sync = fields.Datetime('Last Shopify Sync')
  shopify_sync_error = fields.Text('Sync Error Message')
  shopify_payment_status = fields.Selection([
      ('pending', 'Pending'),
      ('authorized', 'Authorized'),
      ('partially_paid', 'Partially Paid'),
      ('paid', 'Paid'),
      ('partially_refunded', 'Partially Refunded'),
      ('refunded', 'Refunded'),
      ('voided', 'Voided'),
  ],
                                            string='Shopify Payment Status')
  shopify_payment_method = fields.Char('Shopify Payment Method')
  shopify_fulfillment_status = fields.Selection([
      ('unfulfilled', 'Unfulfilled'),
      ('partial', 'Partially Fulfilled'),
      ('fulfilled', 'Fulfilled'),
      ('restocked', 'Restocked'),
  ],
                                                string='Shopify Fulfillment Status')
  shopify_delivery_category = fields.Selection([
      ('shipping', 'Shipping'),
      ('pickup', 'Store Pickup'),
      ('local_delivery', 'Local Delivery'),
      ('none', 'Not Required'),
  ],
                                               string='Shopify Delivery Type')
  shopify_delivery_method = fields.Char('Shopify Delivery Method')

  config_id = fields.Many2one(
      'pos.config',
      string='POS Configuration',
      help='POS configuration used for this order, if applicable',
  )

  @api.model_create_multi
  def create(self, vals_list):
    """Override create to handle Shopify sync for new orders"""
    orders = super().create(vals_list)

    # Handle auto-sync for orders created in Odoo
    for order in orders:
      if order.shopify_sync_enabled and order.shopify_order_source == 'odoo':
        self._create_shopify_mappings(order)

    return orders

  def write(self, vals):
    """Override write to sync changes to Shopify if mappings exist"""
    result = super().write(vals)

    # If shopify_sync_enabled is being turned on, create mappings
    if vals.get('shopify_sync_enabled'):
      for order in self:
        if order.shopify_order_source == 'odoo':
          self._create_shopify_mappings(order)

    # If order details are updated and sync is enabled, mark for re-export
    sync_fields = ['partner_id', 'order_line', 'amount_total', 'state']
    if any(field in vals for field in sync_fields):
      for order in self:
        if order.shopify_sync_enabled and order.shopify_order_source == 'odoo':
          self._mark_for_resync(order)

    return result

  def _create_shopify_mappings(self, order):
    """Create Shopify order mappings for all active instances"""
    active_instances = self.env['shopify.instance'].search([('active', '=', True),
                                                            ('state', '=', 'connected')])

    for instance in active_instances:
      # Check if mapping already exists
      existing_mapping = self.env['shopify.order'].search([('odoo_order_id', '=', order.id),
                                                           ('instance_id', '=', instance.id)],
                                                          limit=1)

      if not existing_mapping:
        # Create mapping with pending status
        self.env['shopify.order'].create({
            'shopify_order_id': '',  # Will be filled after export
            'odoo_order_id': order.id,
            'instance_id': instance.id,
            'sync_status': 'pending',
        })

  def _mark_for_resync(self, order):
    """Mark existing Shopify mappings for re-sync"""
    mappings = self.env['shopify.order'].search([('odoo_order_id', '=', order.id)])

    for mapping in mappings:
      mapping.write({
          'sync_status': 'pending',
      })

  def action_enable_shopify_sync(self):
    """Action to enable Shopify sync for selected orders"""
    for order in self:
      order.shopify_sync_enabled = True

    return {
        'type': 'ir.actions.client',
        'tag': 'display_notification',
        'params': {
            'title':
                'Shopify Sync',
            'message':
                f'Shopify sync enabled for {len(self)} order(s). Export will be scheduled automatically.',
            'type':
                'success',
            'sticky':
                False,
        },
    }

  def action_disable_shopify_sync(self):
    """Action to disable Shopify sync for selected orders"""
    for order in self:
      order.shopify_sync_enabled = False

    return {
        'type': 'ir.actions.client',
        'tag': 'display_notification',
        'params': {
            'title': 'Shopify Sync',
            'message': f'Shopify sync disabled for {len(self)} order(s).',
            'type': 'info',
            'sticky': False,
        },
    }

  def _set_shopify_status_values(self, status_vals):
    """Utility to update Shopify status helper fields safely."""
    allowed_keys = {
        'shopify_payment_status',
        'shopify_payment_method',
        'shopify_fulfillment_status',
        'shopify_delivery_category',
        'shopify_delivery_method',
    }
    filtered_vals = {k: v for k, v in status_vals.items() if k in allowed_keys}
    if filtered_vals:
      self.write(filtered_vals)

  def action_import_from_shopify(self):
    """Action to manually import orders from all Shopify instances"""
    active_instances = self.env['shopify.instance'].search([('active', '=', True),
                                                            ('state', '=', 'connected')])

    if not active_instances:
      return {
          'type': 'ir.actions.client',
          'tag': 'display_notification',
          'params': {
              'title': 'Shopify Import',
              'message': 'No active Shopify instances found.',
              'type': 'warning',
              'sticky': False,
          },
      }

    imported_count = 0
    for instance in active_instances:
      try:
        orders = self.env['shopify.order'].import_orders_from_shopify(instance)
        imported_count += len(orders) if orders else 0
      except Exception as e:
        _logger.error(f"Failed to import orders from {instance.name}: {str(e)}")

    return {
        'type': 'ir.actions.client',
        'tag': 'display_notification',
        'params': {
            'title': 'Shopify Import',
            'message': f'Import process completed. Check logs for details.',
            'type': 'success',
            'sticky': False,
        },
    }
