from odoo import models, fields, api
import logging

_logger = logging.getLogger(__name__)


class ProductProduct(models.Model):
  _inherit = 'product.product'

  shopify_sync_enabled = fields.Boolean(
      'Enable Shopify Sync',
      default=False,
      help='If enabled, this product will be automatically synced to all active Shopify instances')
  shopify_external_id = fields.Char('Shopify Variant ID',
                                    help='External Shopify Variant ID for synchronization',
                                    index=True,
                                    copy=False)

  @api.model_create_multi
  def create(self, vals_list):
    """Override create to auto-create Shopify mappings if sync is enabled"""
    products = super().create(vals_list)

    # Check for auto-sync settings
    for product in products:
      if product.shopify_sync_enabled:
        self._create_shopify_mappings(product)

    return products

  def write(self, vals):
    """Override write to sync changes to Shopify if mappings exist"""
    result = super().write(vals)

    # If shopify_sync_enabled is being turned on, create mappings
    if vals.get('shopify_sync_enabled'):
      for product in self:
        self._create_shopify_mappings(product)

    # If product details are updated and sync is enabled, mark for re-export
    sync_fields = ['name', 'list_price', 'default_code', 'description', 'active', 'image_1920']
    if any(field in vals for field in sync_fields):
      for product in self:
        if product.shopify_sync_enabled:
          self._mark_for_resync(product)

    return result

  def _create_shopify_mappings(self, product):
    """Create Shopify product mappings for all active instances"""
    active_instances = self.env['shopify.instance'].search([('active', '=', True),
                                                            ('state', '=', 'connected')])

    for instance in active_instances:
      # Check if mapping already exists
      existing_mapping = self.env['shopify.product'].search([('odoo_product_id', '=', product.id),
                                                             ('instance_id', '=', instance.id)],
                                                            limit=1)

      if not existing_mapping:
        # Create mapping with pending status
        self.env['shopify.product'].create({
            'name': product.name,
            'shopify_product_id': '',  # Will be filled after export
            'odoo_product_id': product.id,
            'instance_id': instance.id,
            'sync_status': 'pending',
        })

        _logger.info(
            f"Created pending Shopify mapping for product {product.name} on instance {instance.name}"
        )

  def _mark_for_resync(self, product):
    """Mark existing Shopify mappings for re-sync"""
    mappings = self.env['shopify.product'].search([('odoo_product_id', '=', product.id)])

    for mapping in mappings:
      mapping.write({
          'sync_status': 'pending',
          'name': product.name,  # Update the mapping name too
      })

    if mappings:
      _logger.info(
          f"Marked {len(mappings)} Shopify mappings for re-sync for product {product.name}")

  def action_enable_shopify_sync(self):
    """Action to enable Shopify sync for selected products"""
    for product in self:
      product.shopify_sync_enabled = True

    return {
        'type': 'ir.actions.client',
        'tag': 'display_notification',
        'params': {
            'title':
                'Shopify Sync',
            'message':
                f'Shopify sync enabled for {len(self)} product(s). Mappings will be created automatically.',
            'type':
                'success',
            'sticky':
                False,
        },
    }

  def action_disable_shopify_sync(self):
    """Action to disable Shopify sync for selected products"""
    for product in self:
      product.shopify_sync_enabled = False

    return {
        'type': 'ir.actions.client',
        'tag': 'display_notification',
        'params': {
            'title': 'Shopify Sync',
            'message': f'Shopify sync disabled for {len(self)} product(s).',
            'type': 'info',
            'sticky': False,
        },
    }

  def action_sync_to_shopify(self):
    """Action to manually sync products to Shopify"""
    active_instances = self.env['shopify.instance'].search([('active', '=', True),
                                                            ('state', '=', 'connected')])

    if not active_instances:
      return {
          'type': 'ir.actions.client',
          'tag': 'display_notification',
          'params': {
              'title': 'Shopify Sync',
              'message': 'No active Shopify instances found.',
              'type': 'warning',
              'sticky': False,
          },
      }

    synced_count = 0
    for product in self:
      for instance in active_instances:
        try:
          self.env['shopify.product'].export_single_product_to_shopify(instance, product)
          synced_count += 1
        except Exception as e:
          _logger.error(f"Failed to sync product {product.name} to {instance.name}: {str(e)}")

    return {
        'type': 'ir.actions.client',
        'tag': 'display_notification',
        'params': {
            'title': 'Shopify Sync',
            'message': f'Initiated sync for {synced_count} product-instance combinations.',
            'type': 'success',
            'sticky': False,
        },
    }
