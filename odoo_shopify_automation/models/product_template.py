from odoo import models, fields, api
import logging

_logger = logging.getLogger(__name__)


class ProductTemplate(models.Model):
  _inherit = 'product.template'

  shopify_sync_enabled = fields.Boolean(
      'Enable Shopify Sync',
      default=False,
      help=
      'If enabled, this product template will be automatically synced to all active Shopify instances'
  )
  shopify_external_id = fields.Char('Shopify Product ID',
                                    help='External Shopify Product ID for synchronization',
                                    index=True,
                                    copy=False)

  @api.model_create_multi
  def create(self, vals_list):
    """Override create to auto-create Shopify mappings if sync is enabled"""
    templates = super().create(vals_list)

    # Check for auto-sync settings
    for template in templates:
      if template.shopify_sync_enabled:
        self._create_shopify_mappings(template)

    return templates

  def write(self, vals):
    """Override write to sync changes to Shopify if mappings exist"""
    result = super().write(vals)

    # If shopify_sync_enabled is being turned on, create mappings
    if vals.get('shopify_sync_enabled'):
      for template in self:
        self._create_shopify_mappings(template)

    # If product template details are updated and sync is enabled, mark for re-export
    sync_fields = ['name', 'list_price', 'default_code', 'description', 'active', 'image_1920']
    if any(field in vals for field in sync_fields):
      for template in self:
        if template.shopify_sync_enabled:
          self._mark_for_resync(template)

    return result

  def _create_shopify_mappings(self, template):
    """Create Shopify product mappings for all active instances for all variants"""
    active_instances = self.env['shopify.instance'].search([('active', '=', True),
                                                            ('state', '=', 'connected')])

    for instance in active_instances:
      # Create mappings for all product variants of this template
      for product in template.product_variant_ids:
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
              f"Created pending Shopify mapping for product variant {product.name} (template: {template.name}) on instance {instance.name}"
          )

  def _mark_for_resync(self, template):
    """Mark existing Shopify mappings for re-sync for all variants"""
    for product in template.product_variant_ids:
      mappings = self.env['shopify.product'].search([('odoo_product_id', '=', product.id)])

      for mapping in mappings:
        mapping.write({
            'sync_status': 'pending',
            'name': product.name,  # Update the mapping name too
        })

    total_mappings = sum(
        len(self.env['shopify.product'].search([('odoo_product_id', '=', p.id)]))
        for p in template.product_variant_ids)

    if total_mappings:
      _logger.info(
          f"Marked {total_mappings} Shopify mappings for re-sync for template {template.name}")

  def action_enable_shopify_sync(self):
    """Action to enable Shopify sync for selected product templates"""
    for template in self:
      template.shopify_sync_enabled = True

    return {
        'type': 'ir.actions.client',
        'tag': 'display_notification',
        'params': {
            'title':
                'Shopify Sync',
            'message':
                f'Shopify sync enabled for {len(self)} product template(s). Mappings will be created automatically for all variants.',
            'type':
                'success',
            'sticky':
                False,
        },
    }

  def action_disable_shopify_sync(self):
    """Action to disable Shopify sync for selected product templates"""
    for template in self:
      template.shopify_sync_enabled = False

    return {
        'type': 'ir.actions.client',
        'tag': 'display_notification',
        'params': {
            'title': 'Shopify Sync',
            'message': f'Shopify sync disabled for {len(self)} product template(s).',
            'type': 'info',
            'sticky': False,
        },
    }

  def action_sync_to_shopify(self):
    """Action to manually sync product templates (all variants) to Shopify"""
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
    for template in self:
      for product in template.product_variant_ids:
        for instance in active_instances:
          try:
            self.env['shopify.product'].export_single_product_to_shopify(instance, product)
            synced_count += 1
          except Exception as e:
            _logger.error(
                f"Failed to sync product {product.name} (template: {template.name}) to {instance.name}: {str(e)}"
            )

    return {
        'type': 'ir.actions.client',
        'tag': 'display_notification',
        'params': {
            'title': 'Shopify Sync',
            'message': f'Initiated sync for {synced_count} product variant-instance combinations.',
            'type': 'success',
            'sticky': False,
        },
    }
