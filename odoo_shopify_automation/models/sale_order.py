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

  shopify_export_status = fields.Selection([
      ('not_exported', 'Not Exported'),
      ('pending', 'Pending Export'),
      ('exported', 'Exported'),
      ('error', 'Export Error'),
  ],
                                           string='Shopify Export Status',
                                           default='not_exported')

  shopify_last_sync = fields.Datetime('Last Shopify Sync')
  shopify_sync_error = fields.Text('Sync Error Message')

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

        # Mark order for export
        order.write({
            'shopify_export_status': 'pending',
        })

        _logger.info(
            f"Created pending Shopify mapping for order {order.name} on instance {instance.name}")

  def _mark_for_resync(self, order):
    """Mark existing Shopify mappings for re-sync"""
    mappings = self.env['shopify.order'].search([('odoo_order_id', '=', order.id)])

    for mapping in mappings:
      mapping.write({
          'sync_status': 'pending',
      })

    if mappings:
      order.write({
          'shopify_export_status': 'pending',
      })
      _logger.info(f"Marked {len(mappings)} Shopify mappings for re-sync for order {order.name}")

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

  def action_export_to_shopify(self):
    """Action to manually export orders to Shopify"""
    active_instances = self.env['shopify.instance'].search([('active', '=', True),
                                                            ('state', '=', 'connected')])

    if not active_instances:
      return {
          'type': 'ir.actions.client',
          'tag': 'display_notification',
          'params': {
              'title': 'Shopify Export',
              'message': 'No active Shopify instances found.',
              'type': 'warning',
              'sticky': False,
          },
      }

    exported_count = 0
    error_count = 0

    for order in self:
      if order.shopify_order_source == 'shopify':
        continue  # Skip orders that came from Shopify

      for instance in active_instances:
        try:
          self._export_single_order_to_shopify(instance, order)
          exported_count += 1
        except Exception as e:
          error_count += 1
          _logger.error(f"Failed to export order {order.name} to {instance.name}: {str(e)}")

    message = f'Export initiated for {exported_count} order-instance combinations.'
    if error_count > 0:
      message += f' {error_count} exports failed.'

    return {
        'type': 'ir.actions.client',
        'tag': 'display_notification',
        'params': {
            'title': 'Shopify Export',
            'message': message,
            'type': 'success' if error_count == 0 else 'warning',
            'sticky': False,
        },
    }

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

  def _export_single_order_to_shopify(self, instance, order):
    """Export a single order to Shopify"""
    if not instance or not order:
      return False

    # Check if order already has a mapping
    existing_mapping = self.env['shopify.order'].search([('odoo_order_id', '=', order.id),
                                                         ('instance_id', '=', instance.id)],
                                                        limit=1)

    if existing_mapping and existing_mapping.shopify_order_id:
      # Update existing order in Shopify
      return self._update_order_in_shopify(instance, order, existing_mapping)
    else:
      # Create new order in Shopify
      return self._create_order_in_shopify(instance, order, existing_mapping)

  def _create_order_in_shopify(self, instance, order, mapping=None):
    """Create a new order in Shopify"""
    url = f"{instance.shop_url}/admin/api/2024-01/orders.json"

    # Prepare order data
    order_data = {
        "order": {
            "email": order.partner_id.email or "",
            "financial_status": "paid" if order.invoice_status == 'invoiced' else "pending",
            "fulfillment_status": "fulfilled" if order.state == 'done' else None,
            "send_receipt": False,
            "send_fulfillment_receipt": False,
            "note": order.note or f"Exported from Odoo Order {order.name}",
            "line_items": [],
            "customer": {
                "first_name":
                    order.partner_id.name.split(' ')[0] if order.partner_id.name else "",
                "last_name":
                    ' '.join(order.partner_id.name.split(' ')[1:])
                    if order.partner_id.name and len(order.partner_id.name.split(' ')) > 1 else "",
                "email":
                    order.partner_id.email or "",
            }
        }
    }

    # Add order lines
    for line in order.order_line:
      if line.product_id.type == 'service':
        continue  # Skip service products

      # Try to find Shopify product mapping
      shopify_product = self.env['shopify.product'].search(
          [('odoo_product_id', '=', line.product_id.id), ('instance_id', '=', instance.id)],
          limit=1)

      line_item = {
          "quantity": int(line.product_uom_qty),
          "price": str(line.price_unit),
          "title": line.name or line.product_id.name,  # Use title instead of name
          "name": line.name or line.product_id.name,  # Also include name for compatibility
      }

      if shopify_product and shopify_product.shopify_product_id:
        line_item["product_id"] = int(shopify_product.shopify_product_id)
        # If we have a product mapping, remove the custom fields
        if "custom" in line_item:
          del line_item["custom"]
      else:
        # If no mapping, send as custom line item with required fields
        line_item["custom"] = True
        line_item["requires_shipping"] = False  # Assume no shipping for custom items

      order_data["order"]["line_items"].append(line_item)

    try:
      response = requests.post(url,
                               json=order_data,
                               auth=(instance.api_key, instance.password),
                               timeout=30)

      if response.status_code == 201:
        shopify_order = response.json().get('order', {})
        shopify_order_id = str(shopify_order.get('id', ''))

        # Update or create mapping
        if mapping:
          mapping.write({
              'shopify_order_id': shopify_order_id,
              'sync_status': 'synced',
              'last_sync': fields.Datetime.now(),
          })
        else:
          self.env['shopify.order'].create({
              'shopify_order_id': shopify_order_id,
              'odoo_order_id': order.id,
              'instance_id': instance.id,
              'sync_status': 'synced',
              'last_sync': fields.Datetime.now(),
          })

        # Update order status
        order.write({
            'shopify_export_status': 'exported',
            'shopify_last_sync': fields.Datetime.now(),
            'shopify_sync_error': False,
        })

        _logger.info(
            f"Successfully exported order {order.name} to Shopify instance {instance.name}")
        return True
      else:
        error_msg = f"Failed to export order: HTTP {response.status_code} - {response.text}"
        order.write({
            'shopify_export_status': 'error',
            'shopify_sync_error': error_msg,
        })
        _logger.error(f"Export failed for order {order.name}: {error_msg}")
        return False

    except Exception as e:
      error_msg = f"Exception during export: {str(e)}"
      order.write({
          'shopify_export_status': 'error',
          'shopify_sync_error': error_msg,
      })
      _logger.error(f"Export exception for order {order.name}: {error_msg}")
      return False

  def _update_order_in_shopify(self, instance, order, mapping):
    """Update an existing order in Shopify"""
    # Note: Shopify has limited order update capabilities
    # Most fields cannot be updated after order creation
    # This is mainly for status updates

    url = f"{instance.shop_url}/admin/api/2024-01/orders/{mapping.shopify_order_id}.json"

    # Prepare update data (limited fields that can be updated)
    update_data = {
        "order": {
            "id": int(mapping.shopify_order_id),
            "note": order.note or f"Updated from Odoo Order {order.name}",
        }
    }

    try:
      response = requests.put(url,
                              json=update_data,
                              auth=(instance.api_key, instance.password),
                              timeout=30)

      if response.status_code == 200:
        mapping.write({
            'sync_status': 'synced',
            'last_sync': fields.Datetime.now(),
        })

        order.write({
            'shopify_export_status': 'exported',
            'shopify_last_sync': fields.Datetime.now(),
            'shopify_sync_error': False,
        })

        _logger.info(f"Successfully updated order {order.name} in Shopify instance {instance.name}")
        return True
      else:
        error_msg = f"Failed to update order: HTTP {response.status_code} - {response.text}"
        order.write({
            'shopify_export_status': 'error',
            'shopify_sync_error': error_msg,
        })
        _logger.error(f"Update failed for order {order.name}: {error_msg}")
        return False

    except Exception as e:
      error_msg = f"Exception during update: {str(e)}"
      order.write({
          'shopify_export_status': 'error',
          'shopify_sync_error': error_msg,
      })
      _logger.error(f"Update exception for order {order.name}: {error_msg}")
      return False
