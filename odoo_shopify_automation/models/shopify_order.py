from odoo import models, fields, api
import requests
import logging
from odoo.exceptions import UserError
from odoo.tools.translate import _

_logger = logging.getLogger(__name__)


class ShopifyOrder(models.Model):
  _name = 'shopify.order'
  _description = 'Shopify Order Mapping'
  _rec_name = 'shopify_order_id'

  shopify_order_id = fields.Char('Shopify Order ID', required=True)
  odoo_order_id = fields.Many2one('sale.order', string='Odoo Sale Order', required=True)
  instance_id = fields.Many2one('shopify.instance', string='Shopify Instance', required=True)
  sync_status = fields.Selection(
      [
          ('synced', 'Synced'),
          ('pending', 'Pending'),
          ('error', 'Error'),
      ],
      string='Sync Status',
      default='pending',
  )
  last_sync = fields.Datetime('Last Sync')
  active = fields.Boolean('Active', default=True)
  note = fields.Text('Notes')

  # Delivery tracking
  tracking_number = fields.Char('Tracking Number', help='Shipment tracking number')
  tracking_url = fields.Char('Tracking URL', help='URL to track the shipment')
  shipped_date = fields.Datetime('Shipped Date', help='Date when order was shipped')

  # Shopify fulfillment ID for tracking
  shopify_fulfillment_id = fields.Char('Shopify Fulfillment ID', help='Fulfillment ID from Shopify')

  _sql_constraints = [
      ('uniq_shopify_order_instance', 'unique(shopify_order_id, instance_id)',
       'This Shopify order is already mapped for this instance!'),
  ]

  def _get_shipping_product(self):
    """Return a service product used to represent Shopify shipping fees."""
    shipping_product = self.env.ref('delivery.product_product_delivery', raise_if_not_found=False)
    if shipping_product:
      return shipping_product

    shipping_product = self.env['product.product'].search([
        ('type', '=', 'service'),
        ('default_code', '=', 'SHOPIFY-SHIPPING'),
    ],
                                                          limit=1)

    if not shipping_product:
      shipping_product = self.env['product.product'].create({
          'name': 'Shopify Shipping',
          'type': 'service',
          'default_code': 'SHOPIFY-SHIPPING',
          'list_price': 0.0,
      })

    return shipping_product

  def _get_tax_product(self):
    tax_product = self.env['product.product'].search([
        ('type', '=', 'service'),
        ('default_code', '=', 'SHOPIFY-TAX'),
    ],
                                                     limit=1)
    if not tax_product:
      tax_product = self.env['product.product'].create({
          'name': 'Shopify Taxes',
          'type': 'service',
          'default_code': 'SHOPIFY-TAX',
          'list_price': 0.0,
      })
    return tax_product

  def _get_discount_product(self):
    discount_product = self.env['product.product'].search([
        ('type', '=', 'service'),
        ('default_code', '=', 'SHOPIFY-DISCOUNT'),
    ],
                                                          limit=1)
    if not discount_product:
      discount_product = self.env['product.product'].create({
          'name': 'Shopify Discount',
          'type': 'service',
          'default_code': 'SHOPIFY-DISCOUNT',
          'list_price': 0.0,
      })
    return discount_product

  def import_orders_from_shopify(self, instance, since_id=None):
    if not instance:
      raise UserError(_('No Shopify instance provided.'))
    base_url = f"{instance.shop_url}/admin/api/2024-10/orders.json"
    headers = {'Content-Type': 'application/json'}
    auth = None
    if getattr(instance, 'access_token', False):
      headers['X-Shopify-Access-Token'] = instance.access_token
    else:
      auth = (instance.api_key, instance.password)

    created_count = 0
    updated_count = 0
    error_count = 0
    total_orders = 0
    last_id = since_id
    all_orders = []

    try:
      while True:
        params = {'limit': 1, 'status': 'any'}
        if last_id:
          params['since_id'] = last_id

        response = requests.get(base_url, headers=headers, auth=auth, params=params, timeout=20)

        if response.status_code != 200:
          raise UserError(
              _(f'Failed to import orders - HTTP {response.status_code}: {response.text}'))

        orders_chunk = response.json().get('orders', [])
        if not orders_chunk:
          break

        total_orders += len(orders_chunk)
        all_orders.extend(orders_chunk)

        for shopify_order in orders_chunk:
          try:
            # Check if order already exists - VALIDATION TO PREVENT DUPLICATES
            existing_mapping = self.search([('shopify_order_id', '=', str(shopify_order['id'])),
                                            ('instance_id', '=', instance.id)],
                                           limit=1)

            # Additional validation: Check if the order number already exists
            if not existing_mapping:
              order_number = shopify_order.get('name') or shopify_order.get('order_number')
              if order_number:
                # Search for orders with the same reference
                existing_order = self.env['sale.order'].search(
                    [('client_order_ref', '=', f"Shopify-{shopify_order['id']}")], limit=1)
                if existing_order:
                  # Find or create mapping for this order
                  existing_mapping = self.search([('odoo_order_id', '=', existing_order.id),
                                                  ('instance_id', '=', instance.id)],
                                                 limit=1)
                  if not existing_mapping:
                    # Create mapping if it doesn't exist
                    existing_mapping = self.create({
                        'shopify_order_id': str(shopify_order['id']),
                        'odoo_order_id': existing_order.id,
                        'instance_id': instance.id,
                        'sync_status': 'synced',
                    })
                  _logger.info(f"Found existing order by reference: Shopify-{shopify_order['id']}")

            currency = False
            currency_code = shopify_order.get('currency') or shopify_order.get(
                'presentment_currency')
            if currency_code:
              currency = self.env['res.currency'].search([('name', '=', currency_code)], limit=1)
              if not currency:
                _logger.warning('Currency %s from Shopify order %s not found in Odoo',
                                currency_code, shopify_order.get('name'))

            # Get or create the Odoo order
            if existing_mapping:
              # Update existing order
              odoo_order = existing_mapping.odoo_order_id
              # if odoo_order:
              #   if currency and odoo_order.currency_id != currency:
              #     odoo_order.write({'currency_id': currency.id})
            else:
              # Create new Odoo sale order
              # Get or create customer
              customer_email = shopify_order.get('email', '') or shopify_order.get(
                  'contact_email', '')
              customer_data = shopify_order.get('customer', {})

              # Try to get customer name from billing address or customer data
              customer_name = 'Unknown Customer'
              billing_address = shopify_order.get('billing_address', {})
              if billing_address:
                first_name = billing_address.get('first_name', '')
                last_name = billing_address.get('last_name', '')
                if first_name or last_name:
                  customer_name = f"{first_name} {last_name}".strip()

              customer = None
              if customer_email:
                customer = self.env['res.partner'].search([('email', '=', customer_email)], limit=1)

              if not customer:
                customer = self.env['res.partner'].create({
                    'name': customer_name,
                    'email': customer_email or '',
                    'is_company': False,
                })

              created_at = shopify_order.get('created_at')
              if created_at:
                # Parse ISO 8601 format and convert to Odoo format
                from datetime import datetime
                try:
                  if 'T' in created_at:
                    # Remove timezone part for now (could be improved later)
                    date_part = created_at.split('T')[0]
                    time_part = created_at.split('T')[1].split('-')[0].split('+')[0]
                    created_at = f"{date_part} {time_part}"
                except:
                  created_at = None

              config_id = self._get_pos_config_for_address(shopify_order)

              warehouse = self._get_pos_warehouse_for_address(shopify_order)

              order_vals = {
                  'partner_id': customer.id if customer else self.env.ref('base.partner_admin').id,
                  'date_order': created_at,
                  'client_order_ref': f"Shopify-{shopify_order['id']}",
                  'note': f"Imported from Shopify Order #{shopify_order['id']}",
                  'shopify_order_source': 'shopify',
                  'shopify_sync_enabled': True,
                  'state': 'draft',
                  'config_id': config_id.id if config_id else False,
                  'warehouse_id': warehouse.id if warehouse else False,
              }
              # if currency:
              #   order_vals['currency_id'] = currency.id
              odoo_order = self.env['sale.order'].create(order_vals)
              created_count += 1

              # Send notification to POS about new Shopify order
              if config_id:
                notification_data = {
                    'order_id': odoo_order.id,
                    'order_name': odoo_order.name,
                    'partner_name': customer.name if customer else 'Unknown',
                    'shopify_order_id': shopify_order['id'],
                    'amount_total': shopify_order.get('total_price', 0),
                }
                config_id._notify_shopify_orders('SHOPIFY_ORDER_CREATE', notification_data)

            if odoo_order.shopify_order_source == 'shopify' and odoo_order.order_line:
              odoo_order.order_line.unlink()

            # Process line items for both new and existing orders
            line_items = shopify_order.get('line_items', [])
            refunded_list = []

            for refund in shopify_order.get('refunds', []):
              for refund_item in refund.get('refund_line_items', []):
                refunded = refund_item.get('line_item').get('product_id', False)
                refunded_list.append(refunded)

            for item in line_items:

              product = None
              product_id = item.get('product_id')

              if product_id in refunded_list:
                continue

              if product_id:
                product = self.env['product.product'].search([
                    ('shopify_product_external_id', '=', str(product_id)),
                    ('active', '=', True),
                ],
                                                             limit=1)

              # Create order line
              line_price = float(item.get('price', 0))
              line_qty = int(item.get('quantity', 1)) or 1
              discount_total = 0.0
              for allocation in item.get('discount_allocations', []) or []:
                try:
                  discount_total += float(allocation.get('amount', 0))
                except (TypeError, ValueError):
                  continue
              if discount_total and line_qty:
                line_price -= (discount_total / line_qty)

              self.env['sale.order.line'].create({
                  'order_id': odoo_order.id,
                  'product_id': product.id,
                  'name': item.get('name', product.name),
                  'product_uom_qty': line_qty,
                  'price_unit': line_price,
                  'tax_id': [(5, 0, 0)],
              })

          except Exception as e:
            error_count += 1

        if len(orders_chunk) < 250:
          break
        # last_id = orders_chunk[-1].get('id')

      return all_orders
    except Exception as e:
      raise UserError(_(f'Exception during order import: {str(e)}'))

  @api.model
  def _run_order_import_cron(self):
    instances = self.env['shopify.instance'].search([('active', '=', True),
                                                     ('state', '=', 'connected')])
    for instance in instances:
      # Get the last imported order ID for this instance to fetch only new orders
      last_order_mapping = self.search([('instance_id', '=', instance.id)],
                                       order='shopify_order_id desc',
                                       limit=1)
      since_id = None
      if last_order_mapping and last_order_mapping.shopify_order_id:
        try:
          since_id = int(last_order_mapping.shopify_order_id)
          _logger.info(
              f"Starting order import for instance {instance.name} from order ID: {since_id}")
        except (ValueError, TypeError):
          _logger.warning(f"Could not parse last order ID: {last_order_mapping.shopify_order_id}")

      self.import_orders_from_shopify(instance, since_id=since_id)

  def sync_order_from_shopify(self, instance):
    """Bi-directional sync: Update Odoo order with latest Shopify data"""
    self.ensure_one()

    if not self.shopify_order_id:
      _logger.warning(f"Cannot sync order {self.odoo_order_id.name} - no Shopify order ID")
      return False

    url = f"{instance.shop_url}/admin/api/2024-10/orders/{self.shopify_order_id}.json"

    try:
      headers = {'Content-Type': 'application/json'}
      if hasattr(instance, 'access_token') and instance.access_token:
        headers['X-Shopify-Access-Token'] = instance.access_token
        response = requests.get(url, headers=headers, timeout=20)
      else:
        response = requests.get(url, auth=(instance.api_key, instance.password), timeout=20)

      if response.status_code == 200:
        shopify_order = response.json().get('order', {})

        # Update mapping with latest status from Shopify
        mapping_vals = {
            'sync_status': 'synced',
            'last_sync': fields.Datetime.now(),
        }

        # Update fulfillment information if available
        fulfillments = shopify_order.get('fulfillments', [])
        if fulfillments:
          fulfillment = fulfillments[0]
          mapping_vals['shopify_fulfillment_id'] = str(fulfillment.get('id', ''))
          mapping_vals['tracking_number'] = fulfillment.get('tracking_number', '')

          tracking_urls = fulfillment.get('tracking_urls', [])
          if tracking_urls:
            mapping_vals['tracking_url'] = tracking_urls[0]
          elif fulfillment.get('tracking_url'):
            mapping_vals['tracking_url'] = fulfillment.get('tracking_url')

        self.write(mapping_vals)

        _logger.info(f"Successfully synced order {self.odoo_order_id.name} from Shopify")
        return True
      else:
        _logger.error(f"Failed to fetch order from Shopify: {response.status_code}")
        return False
    except Exception as e:
      _logger.error(f"Exception syncing order from Shopify: {str(e)}")
      return False

  def action_sync_from_shopify(self):
    """Action to sync order from Shopify"""
    for order_mapping in self:
      instance = order_mapping.instance_id
      if instance:
        order_mapping.sync_order_from_shopify(instance)

    return {
        'type': 'ir.actions.client',
        'tag': 'display_notification',
        'params': {
            'title': 'Shopify Sync',
            'message': f'Synced {len(self)} order(s) from Shopify',
            'type': 'success',
            'sticky': False,
        },
    }

  def action_sync_to_shopify(self):
    """Action to sync order to Shopify"""
    for order_mapping in self:
      instance = order_mapping.instance_id
      if instance:
        order_mapping.sync_order_to_shopify(instance)

    return {
        'type': 'ir.actions.client',
        'tag': 'display_notification',
        'params': {
            'title': 'Shopify Sync',
            'message': f'Synced {len(self)} order(s) to Shopify',
            'type': 'success',
            'sticky': False,
        },
    }

  def action_update_fulfillment(self):
    """Action to update fulfillment from Odoo UI"""
    self.ensure_one()
    return {
        'name': 'Update Fulfillment in Shopify',
        'type': 'ir.actions.act_window',
        'res_model': 'shopify.fulfillment.wizard',
        'view_mode': 'form',
        'target': 'new',
        'context': {
            'default_order_mapping_id': self.id,
            'default_instance_id': self.instance_id.id,
        }
    }

  def _get_pos_config_for_address(self, shopify_order):
    location_id = shopify_order.get('location_id', False)

    if not location_id:
      fulfillments = shopify_order.get('fulfillments', [])
      if fulfillments:
        location_id = fulfillments[0].get('location_id', False)
      else:
        location = shopify_order.get('shipping_address', {}).get('city', '')
        if location:
          if location.strip().lower() == "alexandria":
            location_id = 1234567890

    if location_id:
      pos_config = self.env['pos.config'].search([('shopify_location_id', '=', str(location_id))],
                                                 limit=1)

      if pos_config:
        return pos_config

    # Fallback to any active POS config
    return self.env['pos.config'].search([('active', '=', True)], limit=1)

  def _get_pos_warehouse_for_address(self, shopify_order):
    config = self._get_pos_config_for_address(shopify_order)

    if config.picking_type_id:
      return config.picking_type_id.warehouse_id

    raise UserError(_("No warehouse configured for POS config '%s' or Shopify instance.") % config.name)
