from odoo import models, fields, api
import requests
from odoo.exceptions import UserError
from odoo.tools.translate import _


class ShopifyOrder(models.Model):
  _name = 'shopify.order'
  _description = 'Shopify Order Mapping'
  _rec_name = 'shopify_order_id'

  shopify_order_id = fields.Char('Shopify Order ID', required=True)
  odoo_order_id = fields.Many2one('sale.order', string='Odoo Sale Order', required=True)
  instance_id = fields.Many2one('shopify.instance', string='Shopify Instance', required=True)
  sync_status = fields.Selection([
      ('synced', 'Synced'),
      ('pending', 'Pending'),
      ('error', 'Error'),
  ],
                                 string='Sync Status',
                                 default='pending')
  last_sync = fields.Datetime('Last Sync')
  active = fields.Boolean('Active', default=True)
  note = fields.Text('Notes')

  _sql_constraints = [
      ('uniq_shopify_order_instance', 'unique(shopify_order_id, instance_id)',
       'This Shopify order is already mapped for this instance!'),
  ]

  def import_orders_from_shopify(self, instance):
    """
        Import orders from Shopify for the given instance.
        Creates queue jobs and logs results.
        """
    if not instance:
      raise UserError(_('No Shopify instance provided.'))
    url = f"{instance.shop_url}/admin/api/2024-01/orders.json"
    try:
      response = requests.get(url, auth=(instance.api_key, instance.password), timeout=20)
      if response.status_code == 200:
        orders = response.json().get('orders', [])
        job = self.env['shopify.queue.job'].create({
            'name': f'Import Orders ({instance.name})',
            'job_type': 'import_order',
            'instance_id': instance.id,
            'status': 'in_progress',
        })
        self.env['shopify.log'].create({
            'name':
                'Order Import Started',
            'log_type':
                'info',
            'job_id':
                job.id,
            'message':
                f'Starting import of {len(orders)} orders from Shopify instance {instance.name}',
        })

        # Process each order
        created_count = 0
        updated_count = 0
        error_count = 0

        for shopify_order in orders:
          try:
            # Check if order already exists
            existing_mapping = self.search([('shopify_order_id', '=', str(shopify_order['id'])),
                                            ('instance_id', '=', instance.id)])

            # Get or create the Odoo order
            if existing_mapping:
              # Update existing order
              odoo_order = existing_mapping.odoo_order_id
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

              # Create sale order
              # Convert Shopify date format to Odoo format
              created_at = shopify_order.get('created_at')
              if created_at:
                # Parse ISO 8601 format and convert to Odoo format
                from datetime import datetime
                try:
                  # Handle timezone format like '2025-10-05T13:24:00-04:00'
                  if 'T' in created_at:
                    # Remove timezone part for now (could be improved later)
                    date_part = created_at.split('T')[0]
                    time_part = created_at.split('T')[1].split('-')[0].split('+')[0]
                    created_at = f"{date_part} {time_part}"
                except:
                  created_at = None

              order_vals = {
                  'partner_id': customer.id if customer else self.env.ref('base.partner_admin').id,
                  'date_order': created_at,
                  'client_order_ref': f"Shopify-{shopify_order['id']}",
                  'note': f"Imported from Shopify Order #{shopify_order['id']}",
                  'shopify_order_source': 'shopify',
                  'shopify_sync_enabled': True,
                  'shopify_export_status': 'not_exported',  # Don't export back to Shopify
              }
              odoo_order = self.env['sale.order'].create(order_vals)
              created_count += 1

            # Process line items for both new and existing orders
            line_items = shopify_order.get('line_items', [])
            existing_lines = len(odoo_order.order_line)

            # Add line items if they don't exist or if we're updating
            if existing_lines == 0 and len(line_items) > 0:
              for item in line_items:

                product = None
                product_id = item.get('product_id')

                # Try to find product by Shopify product ID if it exists
                if product_id:
                  product_mapping = self.env['shopify.product'].search(
                      [('shopify_product_id', '=', str(product_id)),
                       ('instance_id', '=', instance.id)],
                      limit=1)
                  if product_mapping:
                    product = product_mapping.odoo_product_id

                # Create placeholder product if not found
                if not product:
                  product_name = item.get('name', 'Unknown Product')
                  product_sku = item.get('sku',
                                         f"SHOPIFY-{shopify_order['id']}-{item.get('id', 'ITEM')}")

                  # Check if product already exists by name or SKU
                  existing_product = self.env['product.product'].search(
                      ['|', ('name', '=', product_name), ('default_code', '=', product_sku)],
                      limit=1)

                  if existing_product:
                    product = existing_product
                  else:
                    product = self.env['product.product'].create({
                        'name': product_name,
                        'default_code': product_sku,
                        'list_price': float(item.get('price', 0)),
                        'type': 'consu',  # consumable product type
                        'categ_id': self.env.ref('product.product_category_all').id,
                    })

                # Create order line
                line_price = float(item.get('price', 0))
                line_qty = int(item.get('quantity', 1))

                self.env['sale.order.line'].create({
                    'order_id': odoo_order.id,
                    'product_id': product.id,
                    'name': item.get('name', product.name),
                    'product_uom_qty': line_qty,
                    'price_unit': line_price,
                })

            # Create or update mapping (for all orders, not just new ones)
            mapping_vals = {
                'shopify_order_id': str(shopify_order['id']),
                'odoo_order_id': odoo_order.id,
                'instance_id': instance.id,
                'sync_status': 'synced',
                'last_sync': fields.Datetime.now(),
            }

            if existing_mapping:
              existing_mapping.write(mapping_vals)
              updated_count += 1
            else:
              self.create(mapping_vals)

          except Exception as e:
            error_count += 1
            self.env['shopify.log'].create({
                'name': 'Order Import Error',
                'log_type': 'error',
                'job_id': job.id,
                'message': f'Error importing order {shopify_order.get("id", "Unknown")}: {str(e)}',
            })

        # Update job status
        job.write({'status': 'done'})
        self.env['shopify.log'].create({
            'name':
                'Order Import Completed',
            'log_type':
                'info',
            'job_id':
                job.id,
            'message':
                f'Import completed: {created_count} created, {updated_count} updated, {error_count} errors',
        })

        return orders
      else:
        job = self.env['shopify.queue.job'].create({
            'name': f'Import Orders ({instance.name})',
            'job_type': 'import_order',
            'instance_id': instance.id,
            'status': 'failed',
            'error_message': response.text,
        })
        self.env['shopify.log'].create({
            'name': 'Order Import Error',
            'log_type': 'error',
            'job_id': job.id,
            'message': f'Failed to import orders: {response.text}',
        })
        raise UserError(_(f'Failed to import orders: {response.text}'))
    except Exception as e:
      job = self.env['shopify.queue.job'].create({
          'name': f'Import Orders ({instance.name})',
          'job_type': 'import_order',
          'instance_id': instance.id,
          'status': 'failed',
          'error_message': str(e),
      })
      self.env['shopify.log'].create({
          'name': 'Order Import Exception',
          'log_type': 'error',
          'job_id': job.id,
          'message': str(e),
      })
      raise UserError(_(f'Exception during order import: {str(e)}'))

  def export_orders_to_shopify(self, instance, orders):
    """
        Export orders to Shopify for the given instance.
        Creates queue jobs and logs results.
        """
    if not instance:
      raise UserError(_('No Shopify instance provided.'))

    job = self.env['shopify.queue.job'].create({
        'name': f'Export Orders ({instance.name})',
        'job_type': 'export_order',
        'instance_id': instance.id,
        'status': 'in_progress',
    })

    self.env['shopify.log'].create({
        'name': 'Order Export Started',
        'log_type': 'info',
        'job_id': job.id,
        'message': f'Starting export of {len(orders)} orders to Shopify instance {instance.name}',
    })

    exported_count = 0
    error_count = 0

    for order in orders:
      try:
        # Skip orders that came from Shopify to prevent circular exports
        if hasattr(order, 'shopify_order_source') and order.shopify_order_source == 'shopify':
          continue

        # Use the sale.order export method
        result = order._export_single_order_to_shopify(instance, order)
        if result:
          exported_count += 1
        else:
          error_count += 1

      except Exception as e:
        error_count += 1
        self.env['shopify.log'].create({
            'name': 'Order Export Error',
            'log_type': 'error',
            'job_id': job.id,
            'message': f'Error exporting order {order.name}: {str(e)}',
        })

    # Update job status
    status = 'done' if error_count == 0 else 'failed'
    job.write({'status': status})

    self.env['shopify.log'].create({
        'name': 'Order Export Completed',
        'log_type': 'info',
        'job_id': job.id,
        'message': f'Export completed: {exported_count} exported, {error_count} errors',
    })

    return exported_count > 0

  def export_single_order_to_shopify(self, instance, order):
    """
        Export a single Odoo order to Shopify.
        This method is called from the sale.order model.
        """
    if not instance or not order:
      return False

    # Delegate to the sale.order model's export method
    return order._export_single_order_to_shopify(instance, order)

  @api.model
  def _run_order_import_cron(self):
    """
        Cron job method to automatically import orders from all active Shopify instances.
        """
    instances = self.env['shopify.instance'].search([('active', '=', True),
                                                     ('state', '=', 'connected')])
    for instance in instances:
      try:
        self.import_orders_from_shopify(instance)
      except Exception as e:
        self.env['shopify.log'].create({
            'name': 'Cron Order Import Error',
            'log_type': 'error',
            'message': f'Error importing orders for instance {instance.name}: {str(e)}',
        })

  @api.model
  def _run_order_export_cron(self):
    """
        Cron job method to automatically export pending orders to all active Shopify instances.
        """
    # Find orders that are pending export
    pending_orders = self.env['sale.order'].search([
        ('shopify_sync_enabled', '=', True),
        ('shopify_export_status', '=', 'pending'),
        ('shopify_order_source', '=', 'odoo'),
        ('state', 'in', ['sale'])  # Only export confirmed orders
    ])

    instances = self.env['shopify.instance'].search([('active', '=', True),
                                                     ('state', '=', 'connected')])

    for order in pending_orders:
      for instance in instances:
        try:
          order._export_single_order_to_shopify(instance, order)
        except Exception as e:
          self.env['shopify.log'].create({
              'name':
                  'Cron Order Export Error',
              'log_type':
                  'error',
              'message':
                  f'Error exporting order {order.name} to instance {instance.name}: {str(e)}',
          })
