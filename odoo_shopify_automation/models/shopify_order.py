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

  # Enhanced fields for order management
  shopify_order_status = fields.Selection(
      [
          ('pending', 'Pending'),
          ('authorized', 'Authorized'),
          ('partially_paid', 'Partially Paid'),
          ('paid', 'Paid'),
          ('partially_refunded', 'Partially Refunded'),
          ('refunded', 'Refunded'),
          ('voided', 'Voided'),
      ],
      string='Shopify Financial Status',
      help='Financial status from Shopify',
  )

  shopify_fulfillment_status = fields.Selection(
      [
          ('unfulfilled', 'Unfulfilled'),
          ('partial', 'Partially Fulfilled'),
          ('fulfilled', 'Fulfilled'),
          ('restocked', 'Restocked'),
      ],
      string='Shopify Fulfillment Status',
      help='Fulfillment status from Shopify',
  )

  shopify_refund_amount = fields.Float('Refund Amount', help='Total amount refunded in Shopify')
  shopify_refund_date = fields.Datetime('Refund Date', help='Date when refund was processed')

  # Delivery tracking
  tracking_number = fields.Char('Tracking Number', help='Shipment tracking number')
  tracking_url = fields.Char('Tracking URL', help='URL to track the shipment')
  carrier = fields.Char('Carrier', help='Shipping carrier name')
  shipped_date = fields.Datetime('Shipped Date', help='Date when order was shipped')

  # Shopify fulfillment ID for tracking
  shopify_fulfillment_id = fields.Char('Shopify Fulfillment ID', help='Fulfillment ID from Shopify')

  _sql_constraints = [
      ('uniq_shopify_order_instance', 'unique(shopify_order_id, instance_id)',
       'This Shopify order is already mapped for this instance!'),
  ]

  def _prepare_sale_order_status_vals(self, shopify_order):
    """Extract payment, fulfillment, and delivery metadata for sale orders."""
    financial_status = (shopify_order.get('financial_status') or 'pending').replace(' ', '_')
    fulfillment_status = (shopify_order.get('fulfillment_status') or
                          'unfulfilled').replace(' ', '_')

    payment_methods = list(shopify_order.get('payment_gateway_names') or [])
    for transaction in (shopify_order.get('transactions') or []):
      gateway = transaction.get('gateway')
      if gateway and gateway not in payment_methods:
        payment_methods.append(gateway)
    payment_method = ', '.join([pm for pm in payment_methods if pm]) or False

    delivery_category = shopify_order.get('delivery_category')
    shipping_lines = shopify_order.get('shipping_lines') or []
    delivery_method = False
    if shipping_lines:
      delivery_method = shipping_lines[0].get('title') or shipping_lines[0].get('code')
      if not delivery_category:
        delivery_category = 'shipping'

    if not delivery_category:
      requires_shipping = shopify_order.get('requires_shipping')
      if requires_shipping is False:
        delivery_category = 'none'
      else:
        delivery_category = 'shipping'

    financial_map = {
        'pending': 'pending',
        'authorized': 'authorized',
        'partially_paid': 'partially_paid',
        'paid': 'paid',
        'partially_refunded': 'partially_refunded',
        'refunded': 'refunded',
        'voided': 'voided',
    }
    fulfillment_map = {
        'unfulfilled': 'unfulfilled',
        'partial': 'partial',
        'fulfilled': 'fulfilled',
        'restocked': 'restocked',
    }
    delivery_map = {
        'shipping': 'shipping',
        'pickup': 'pickup',
        'local_delivery': 'local_delivery',
        'none': 'none',
    }

    financial_status = financial_map.get(financial_status, 'pending')
    fulfillment_status = fulfillment_map.get(fulfillment_status, 'unfulfilled')
    delivery_category = delivery_map.get(delivery_category, 'shipping')

    return {
        'shopify_payment_status': financial_status,
        'shopify_payment_method': payment_method,
        'shopify_fulfillment_status': fulfillment_status,
        'shopify_delivery_category': delivery_category,
        'shopify_delivery_method': delivery_method,
    }

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

  def _get_refund_product(self):
    refund_product = self.env['product.product'].search([
        ('type', '=', 'service'),
        ('default_code', '=', 'SHOPIFY-REFUND'),
    ],
                                                        limit=1)
    if not refund_product:
      refund_product = self.env['product.product'].create({
          'name': 'Shopify Refund',
          'type': 'service',
          'default_code': 'SHOPIFY-REFUND',
          'list_price': 0.0,
      })
    return refund_product

  def _get_sale_journal(self, company):
    journal = self.env['account.journal'].search([('type', '=', 'sale'),
                                                  ('company_id', '=', company.id)],
                                                 limit=1)
    if not journal:
      raise UserError(
          _('No sales journal found for company %s. Please configure a sales journal.') %
          company.display_name)
    return journal

  def _ensure_invoice_for_order(self, odoo_order):
    if odoo_order.state in ['draft', 'sent']:
      odoo_order.action_confirm()

    invoice = odoo_order.invoice_ids.filtered(
        lambda inv: inv.move_type == 'out_invoice' and inv.state != 'cancel')[:1]
    if not invoice:
      invoices = odoo_order._create_invoices()
      invoice = invoices[:1]

    if invoice and invoice.state != 'posted':
      invoice.action_post()

    return invoice

  def _sync_shopify_refunds(self,
                            instance,
                            odoo_order,
                            shopify_order,
                            specific_refunds=None,
                            currency=None,
                            invoice=None):
    refunds = specific_refunds or shopify_order.get('refunds', [])
    if not refunds:
      return

    refund_product = self._get_refund_product()
    journal = self._get_sale_journal(odoo_order.company_id)
    currency = currency or odoo_order.currency_id

    invoice = invoice or (odoo_order.invoice_ids.filtered(
        lambda i: i.move_type == 'out_invoice' and i.state != 'cancel')[:1])

    invoice_line_pool = {}
    if invoice:
      for line in invoice.invoice_line_ids:
        invoice_line_pool.setdefault(line.product_id.id, [])
        invoice_line_pool[line.product_id.id].append({
            'line': line,
            'remaining_qty': line.quantity,
        })

    for refund in refunds:
      refund_id = refund.get('id')
      if not refund_id:
        continue
      refund_id = str(refund_id)

      transactions = refund.get('transactions', []) or []
      amount = 0.0
      for tx in transactions:
        try:
          amount += float(tx.get('amount', 0) or 0)
        except (TypeError, ValueError):
          continue
      amount = abs(amount)

      if amount == 0.0:
        try:
          amount = sum(
              abs(float(line.get('subtotal', 0) or 0))
              for line in refund.get('refund_line_items', []) or [])
        except (TypeError, ValueError):
          amount = 0.0

      mapping = self.env['shopify.refund'].search([('shopify_refund_id', '=', refund_id),
                                                   ('instance_id', '=', instance.id)],
                                                  limit=1)
      move = mapping.move_id if mapping else False

      invoice_line_vals = []
      constructed_total = 0.0

      refund_line_items = refund.get('refund_line_items', []) or []
      for refund_line in refund_line_items:
        qty = refund_line.get('quantity')
        try:
          qty = abs(float(qty))
        except (TypeError, ValueError):
          qty = 0.0
        if not qty:
          continue

        line_item_data = refund_line.get('line_item') or {}
        product = None
        product_id = line_item_data.get('product_id')
        if product_id:
          mapping_product = self.env['shopify.product'].search(
              [('shopify_product_id', '=', str(product_id)), ('instance_id', '=', instance.id)],
              limit=1)
          if mapping_product:
            product = mapping_product.odoo_product_id

        if not product:
          product_name = line_item_data.get('name', 'Shopify Item')
          product_sku = line_item_data.get('sku')
          product = self.env['product.product'].search(
              ['|', ('name', '=', product_name), ('default_code', '=', product_sku)], limit=1)
          if not product:
            product = self.env['product.product'].create({
                'name': product_name,
                'default_code': product_sku,
                'type': 'consu',
                'list_price': float(line_item_data.get('price', 0) or 0),
                'categ_id': self.env.ref('product.product_category_all').id,
            })

        price_unit = 0.0
        try:
          price_unit = float(refund_line.get('subtotal', 0) or 0) / qty
        except (TypeError, ValueError, ZeroDivisionError):
          price_unit = 0.0

        if not price_unit and line_item_data.get('price'):
          try:
            price_unit = float(line_item_data.get('price'))
          except (TypeError, ValueError):
            price_unit = 0.0

        tax_amount = 0.0
        try:
          tax_amount = float(refund_line.get('total_tax', 0) or 0)
        except (TypeError, ValueError):
          tax_amount = 0.0
        if tax_amount and qty:
          price_unit += tax_amount / qty

        tax_ids = []
        invoice_line = False
        if invoice:
          candidates = invoice_line_pool.get(product.id, [])
          for candidate in candidates:
            if candidate['remaining_qty'] >= qty - 1e-6:
              invoice_line = candidate['line']
              candidate['remaining_qty'] -= qty
              break
          if invoice_line:
            tax_ids = invoice_line.tax_ids.ids
            if not price_unit:
              price_unit = invoice_line.price_unit

        if not price_unit and invoice_line:
          price_unit = invoice_line.price_unit

        constructed_total += price_unit * qty
        invoice_line_vals.append((0, 0, {
            'product_id': product.id,
            'name': line_item_data.get('name', product.name),
            'quantity': qty,
            'price_unit': price_unit,
            'tax_ids': [(6, 0, tax_ids)] if tax_ids else [(5, 0, 0)],
        }))

      shipping_data = refund.get('shipping') or {}
      shipping_amount = shipping_data.get('amount') or shipping_data.get('maximum')
      try:
        shipping_amount = float(shipping_amount or 0)
      except (TypeError, ValueError):
        shipping_amount = 0.0
      if shipping_amount:
        shipping_product = self._get_shipping_product()
        shipping_tax_ids = []
        if invoice:
          ship_lines = invoice.invoice_line_ids.filtered(lambda l: l.product_id == shipping_product)
          if ship_lines:
            shipping_tax_ids = ship_lines[0].tax_ids.ids
        invoice_line_vals.append((0, 0, {
            'product_id': shipping_product.id,
            'name': shipping_data.get('title') or 'Shipping Refund',
            'quantity': 1,
            'price_unit': shipping_amount,
            'tax_ids': [(6, 0, shipping_tax_ids)] if shipping_tax_ids else [(5, 0, 0)],
        }))
        constructed_total += shipping_amount

      adjustments = refund.get('adjustments') or []
      for adjustment in adjustments:
        adj_amount = adjustment.get('amount')
        try:
          adj_amount = float(adj_amount or 0)
        except (TypeError, ValueError):
          adj_amount = 0.0
        if not adj_amount:
          continue
        invoice_line_vals.append((0, 0, {
            'product_id': refund_product.id,
            'name': adjustment.get('reason') or 'Adjustment',
            'quantity': 1,
            'price_unit': abs(adj_amount),
            'tax_ids': [(5, 0, 0)],
        }))
        constructed_total += abs(adj_amount)

      if not invoice_line_vals:
        invoice_line_vals.append((0, 0, {
            'product_id': refund_product.id,
            'name': refund.get('note') or f"Shopify Refund {refund_id}",
            'quantity': 1,
            'price_unit': amount,
            'tax_ids': [(5, 0, 0)],
        }))
        constructed_total = amount

      delta = amount - constructed_total
      if abs(delta) > 0.01:
        invoice_line_vals.append((0, 0, {
            'product_id': refund_product.id,
            'name': 'Shopify Refund Adjustment',
            'quantity': 1,
            'price_unit': delta,
            'tax_ids': [(5, 0, 0)],
        }))

      move_vals = {
          'move_type': 'out_refund',
          'partner_id': odoo_order.partner_id.id,
          'invoice_origin': odoo_order.name,
          'ref': f"Shopify Refund {refund_id}",
          'currency_id': currency.id,
          'journal_id': journal.id,
          'invoice_date': refund.get('created_at') or fields.Date.today(),
          'invoice_line_ids': invoice_line_vals,
      }

      if invoice and invoice.state != 'cancel':
        move_vals['reversed_entry_id'] = invoice.id

      mapping = self.env['shopify.refund'].search([('shopify_refund_id', '=', refund_id),
                                                   ('instance_id', '=', instance.id)],
                                                  limit=1)
      move = mapping.move_id if mapping else False

      if move and move.state == 'posted':
        move.button_draft()
      if move:
        move.write(move_vals)
      else:
        move = self.env['account.move'].create(move_vals)

      if move.state != 'posted':
        move.action_post()

      if invoice and invoice.state == 'posted':
        receivable_lines = (invoice.line_ids + move.line_ids).filtered(
            lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled)
        if receivable_lines:
          try:
            receivable_lines.reconcile()
          except Exception as reconcile_error:
            _logger.warning('Failed to reconcile refund %s with invoice %s: %s', refund_id,
                            invoice.name, reconcile_error)

      mapping_vals = {
          'name': refund.get('note') or f"Shopify Refund {refund_id}",
          'order_mapping_id': self.id,
          'instance_id': instance.id,
          'move_id': move.id,
          'amount': abs(move.amount_total),
          'currency_id': move.currency_id.id,
          'refund_date': refund.get('created_at'),
          'state': move.state,
      }

      if mapping:
        mapping.write(mapping_vals)
      else:
        mapping_vals.update({'shopify_refund_id': refund_id})
        self.env['shopify.refund'].create(mapping_vals)

  def import_orders_from_shopify(self, instance):
    """
        Import orders from Shopify for the given instance.
        Creates queue jobs and logs results.
        """
    if not instance:
      raise UserError(_('No Shopify instance provided.'))
    base_url = f"{instance.shop_url}/admin/api/2024-01/orders.json"
    headers = {'Content-Type': 'application/json'}
    auth = None
    if getattr(instance, 'access_token', False):
      headers['X-Shopify-Access-Token'] = instance.access_token
    else:
      auth = (instance.api_key, instance.password)

    job = self.env['shopify.queue.job'].create({
        'name': f'Import Orders ({instance.name})',
        'job_type': 'import_order',
        'instance_id': instance.id,
        'status': 'in_progress',
    })
    self.env['shopify.log'].sudo().create({
        'name': 'Order Import Started',
        'log_type': 'info',
        'job_id': job.id,
        'message': f'Starting order import from Shopify instance {instance.name}',
    })

    created_count = 0
    updated_count = 0
    error_count = 0
    total_orders = 0
    last_id = None
    all_orders = []

    try:
      while True:
        params = {'limit': 250, 'status': 'any'}
        if last_id:
          params['since_id'] = last_id

        response = requests.get(base_url, headers=headers, auth=auth, params=params, timeout=20)

        if response.status_code != 200:
          job.write({'status': 'failed', 'error_message': response.text})
          self.env['shopify.log'].create({
              'name': 'Order Import Error',
              'log_type': 'error',
              'job_id': job.id,
              'message': f'Failed to import orders - HTTP {response.status_code}: {response.text}',
          })
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

            status_vals = self._prepare_sale_order_status_vals(shopify_order)

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
              if odoo_order:
                if currency and odoo_order.currency_id != currency:
                  odoo_order.write({'currency_id': currency.id})
                odoo_order._set_shopify_status_values(status_vals)
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
              if currency:
                order_vals['currency_id'] = currency.id
              order_vals.update(status_vals)
              odoo_order = self.env['sale.order'].create(order_vals)
              created_count += 1

            if odoo_order.shopify_order_source == 'shopify' and odoo_order.order_line:
              odoo_order.order_line.unlink()

            # Process line items for both new and existing orders
            line_items = shopify_order.get('line_items', [])
            for item in line_items:

              product = None
              product_id = item.get('product_id')

              # Try to find product by Shopify product ID if it exists
              if product_id:
                product_mapping = self.env['shopify.product'].search(
                    [('shopify_product_id', '=', str(product_id)), ('active', '=', True),
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
                    ['|', ('name', '=', product_name), ('default_code', '=', product_sku)], limit=1)

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

            shipping_lines = shopify_order.get('shipping_lines', [])
            if shipping_lines:
              shipping_product = self._get_shipping_product()
              for shipping_line in shipping_lines:
                shipping_price = float(shipping_line.get('price', 0))
                self.env['sale.order.line'].create({
                    'order_id': odoo_order.id,
                    'product_id': shipping_product.id,
                    'name': shipping_line.get('title') or 'Shipping',
                    'product_uom_qty': 1,
                    'price_unit': shipping_price,
                    'tax_id': [(5, 0, 0)],
                })

            total_tax_amount = float(shopify_order.get('total_tax', 0) or 0)
            if total_tax_amount:
              pass

            total_discount_amount = float(shopify_order.get('total_discounts', 0) or 0)
            if total_discount_amount:
              discount_product = self._get_discount_product()
              self.env['sale.order.line'].create({
                  'order_id': odoo_order.id,
                  'product_id': discount_product.id,
                  'name': 'Shopify Discount',
                  'product_uom_qty': 1,
                  'price_unit': -total_discount_amount,
                  'tax_id': [(5, 0, 0)],
              })

            invoice = False
            if shopify_order.get('cancelled_at'):
              if odoo_order.state not in ['cancel']:
                try:
                  odoo_order.action_cancel()
                except Exception as cancel_error:
                  _logger.warning('Failed to cancel order %s from Shopify: %s', odoo_order.name,
                                  cancel_error)
            else:
              invoice = self._ensure_invoice_for_order(odoo_order)

            # Create or update mapping (for all orders, not just new ones) with enhanced fields
            mapping_vals = {
                'shopify_order_id':
                    str(shopify_order['id']),
                'odoo_order_id':
                    odoo_order.id,
                'instance_id':
                    instance.id,
                'sync_status':
                    'synced',
                'last_sync':
                    fields.Datetime.now(),
                'shopify_order_status':
                    shopify_order.get('financial_status', 'pending'),
                'shopify_fulfillment_status':
                    shopify_order.get('fulfillment_status', 'unfulfilled') or 'unfulfilled',
            }

            # Extract refund information
            refunds = shopify_order.get('refunds', [])
            if refunds:
              total_refund = sum([
                  float(refund.get('transactions', [{}])[0].get('amount', 0))
                  for refund in refunds
                  if refund.get('transactions')
              ])
              if total_refund > 0:
                mapping_vals['shopify_refund_amount'] = total_refund
                mapping_vals['shopify_refund_date'] = fields.Datetime.now()

            # Extract fulfillment/tracking information
            fulfillments = shopify_order.get('fulfillments', [])
            if fulfillments:
              fulfillment = fulfillments[0]  # Get first fulfillment
              mapping_vals['shopify_fulfillment_id'] = str(fulfillment.get('id', ''))
              mapping_vals['carrier'] = fulfillment.get('tracking_company', '')
              mapping_vals['tracking_number'] = fulfillment.get('tracking_number', '')

              # Get tracking URL
              tracking_urls = fulfillment.get('tracking_urls', [])
              if tracking_urls:
                mapping_vals['tracking_url'] = tracking_urls[0]
              elif fulfillment.get('tracking_url'):
                mapping_vals['tracking_url'] = fulfillment.get('tracking_url')

              # Get shipped date
              if fulfillment.get('created_at'):
                from datetime import datetime
                try:
                  date_part = fulfillment['created_at'].split('T')[0]
                  time_part = fulfillment['created_at'].split('T')[1].split('-')[0].split('+')[0]
                  mapping_vals['shipped_date'] = f"{date_part} {time_part}"
                except:
                  pass

            if existing_mapping:
              existing_mapping.write(mapping_vals)
              updated_count += 1
              mapping_record = existing_mapping
              # Create notification for order update

            else:
              new_mapping = self.create(mapping_vals)
              mapping_record = new_mapping


            if mapping_record:
              mapping_record._sync_shopify_refunds(instance,
                                                   odoo_order,
                                                   shopify_order,
                                                   currency=currency or odoo_order.currency_id,
                                                   invoice=invoice)

          except Exception as e:
            error_count += 1
            self.env['shopify.log'].create({
                'name': 'Order Import Error',
                'log_type': 'error',
                'job_id': job.id,
                'message': f'Error importing order {shopify_order.get("id", "Unknown")}: {str(e)}',
            })

        if len(orders_chunk) < 250:
          break
        last_id = orders_chunk[-1].get('id')

      # Update job status
      job.write({'status': 'done'})
      self.env['shopify.log'].sudo().create({
          'name':
              'Order Import Completed',
          'log_type':
              'info',
          'job_id':
              job.id,
          'message':
              f'Import completed: {created_count} created, {updated_count} updated, {error_count} errors',
      })

      return all_orders
    except Exception as e:
      job.write({'status': 'failed', 'error_message': str(e)})
      self.env['shopify.log'].sudo().create({
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

    self.env['shopify.log'].sudo().create({
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
        self.env['shopify.log'].sudo().create({
            'name': 'Order Export Error',
            'log_type': 'error',
            'job_id': job.id,
            'message': f'Error exporting order {order.name}: {str(e)}',
        })

    # Update job status
    status = 'done' if error_count == 0 else 'failed'
    job.write({'status': status})

    self.env['shopify.log'].sudo().create({
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

  def process_refund_in_shopify(self,
                                instance,
                                refund_amount,
                                reason='customer_request',
                                notify_customer=False):
    """Process a refund in Shopify for this order"""
    self.ensure_one()

    if not instance:
      raise UserError(_('No Shopify instance provided.'))

    if not self.shopify_order_id:
      raise UserError(_('This order has not been synced to Shopify yet.'))

    # Get order details from Shopify first
    url = f"{instance.shop_url}/admin/api/2024-07/orders/{self.shopify_order_id}.json"

    headers = {'Content-Type': 'application/json'}
    if hasattr(instance, 'access_token') and instance.access_token:
      headers['X-Shopify-Access-Token'] = instance.access_token
      response = requests.get(url, headers=headers, timeout=20)
    else:
      response = requests.get(url, auth=(instance.api_key, instance.password), timeout=20)

    if response.status_code != 200:
      raise UserError(_(f'Failed to fetch order from Shopify: {response.text}'))

    order_data = response.json().get('order', {})
    line_items = order_data.get('line_items', [])

    # Prepare refund data
    refund_data = {
        'refund': {
            'notify':
                notify_customer,
            'note':
                reason,
            'shipping': {
                'full_refund': False,
                'amount': 0
            },
            'refund_line_items': [],
            'transactions': [{
                'parent_id':
                    order_data.get('transactions', [{}])[0].get('id')
                    if order_data.get('transactions') else None,
                'amount':
                    refund_amount,
                'kind':
                    'refund',
                'gateway':
                    order_data.get('gateway', 'manual')
            }]
        }
    }

    # Add all line items for refund
    for line_item in line_items:
      refund_data['refund']['refund_line_items'].append({
          'line_item_id': line_item['id'],
          'quantity': line_item['quantity'],
          'restock_type': 'return'
      })

    # Post refund to Shopify
    refund_url = f"{instance.shop_url}/admin/api/2024-07/orders/{self.shopify_order_id}/refunds.json"

    try:
      if hasattr(instance, 'access_token') and instance.access_token:
        response = requests.post(refund_url, headers=headers, json=refund_data, timeout=30)
      else:
        response = requests.post(refund_url,
                                 auth=(instance.api_key, instance.password),
                                 json=refund_data,
                                 timeout=30)

      if response.status_code in [200, 201]:
        refund_result = response.json().get('refund', {})

        # Update the mapping with refund information
        self.write({
            'shopify_refund_amount':
                refund_amount,
            'shopify_refund_date':
                fields.Datetime.now(),
            'shopify_order_status':
                'refunded' if refund_amount >= float(order_data.get('total_price', 0)) else
                'partially_refunded',
            'sync_status':
                'synced',
            'last_sync':
                fields.Datetime.now(),
        })
        if self.odoo_order_id:
          payment_status = ('refunded' if refund_amount >= float(order_data.get('total_price', 0))
                            else 'partially_refunded')
          status_vals = self._prepare_sale_order_status_vals(order_data)
          status_vals['shopify_payment_status'] = payment_status
          self.odoo_order_id._set_shopify_status_values(status_vals)

        # Log the refund
        self.env['shopify.log'].create({
            'name':
                f'Refund Processed - Order {self.shopify_order_id}',
            'log_type':
                'info',
            'message':
                f'Refund of {refund_amount} processed successfully for order {self.shopify_order_id}',
        })

        if self.odoo_order_id:
          invoice = self._ensure_invoice_for_order(self.odoo_order_id)
          self._sync_shopify_refunds(instance,
                                     self.odoo_order_id,
                                     order_data,
                                     specific_refunds=[refund_result],
                                     currency=self.odoo_order_id.currency_id,
                                     invoice=invoice)

        return True
      else:
        raise UserError(_(f'Failed to process refund: {response.status_code} - {response.text}'))

    except Exception as e:
      self.env['shopify.log'].create({
          'name': f'Refund Error - Order {self.shopify_order_id}',
          'log_type': 'error',
          'message': f'Error processing refund: {str(e)}',
      })
      raise UserError(_(f'Error processing refund: {str(e)}'))

  def update_fulfillment_in_shopify(self,
                                    instance,
                                    tracking_number=None,
                                    tracking_url=None,
                                    carrier=None,
                                    notify_customer=True):
    """Create or update fulfillment in Shopify with tracking information"""
    self.ensure_one()

    if not instance:
      raise UserError(_('No Shopify instance provided.'))

    if not self.shopify_order_id:
      raise UserError(_('This order has not been synced to Shopify yet.'))

    # Get order line items
    url = f"{instance.shop_url}/admin/api/2024-07/orders/{self.shopify_order_id}.json"

    headers = {'Content-Type': 'application/json'}
    if hasattr(instance, 'access_token') and instance.access_token:
      headers['X-Shopify-Access-Token'] = instance.access_token
      response = requests.get(url, headers=headers, timeout=20)
    else:
      response = requests.get(url, auth=(instance.api_key, instance.password), timeout=20)

    if response.status_code != 200:
      raise UserError(_(f'Failed to fetch order from Shopify: {response.text}'))

    order_data = response.json().get('order', {})
    line_items = order_data.get('line_items', [])

    # Prepare fulfillment data
    fulfillment_data = {
        'fulfillment': {
            'location_id': order_data.get('location_id'),
            'tracking_number': tracking_number or '',
            'tracking_urls': [tracking_url] if tracking_url else [],
            'notify_customer': notify_customer,
            'line_items': []
        }
    }

    # Add tracking company if provided
    if carrier:
      fulfillment_data['fulfillment']['tracking_company'] = carrier

    # Add all line items to fulfillment
    for line_item in line_items:
      fulfillment_data['fulfillment']['line_items'].append({
          'id': line_item['id'],
          'quantity': line_item['fulfillable_quantity']
      })

    # Create fulfillment in Shopify
    fulfillment_url = f"{instance.shop_url}/admin/api/2024-07/orders/{self.shopify_order_id}/fulfillments.json"

    try:
      if hasattr(instance, 'access_token') and instance.access_token:
        response = requests.post(fulfillment_url,
                                 headers=headers,
                                 json=fulfillment_data,
                                 timeout=30)
      else:
        response = requests.post(fulfillment_url,
                                 auth=(instance.api_key, instance.password),
                                 json=fulfillment_data,
                                 timeout=30)

      if response.status_code in [200, 201]:
        fulfillment_result = response.json().get('fulfillment', {})

        # Update the mapping with fulfillment information
        self.write({
            'shopify_fulfillment_id': str(fulfillment_result.get('id', '')),
            'tracking_number': tracking_number or '',
            'tracking_url': tracking_url or '',
            'carrier': carrier or '',
            'shipped_date': fields.Datetime.now(),
            'shopify_fulfillment_status': 'fulfilled',
            'sync_status': 'synced',
            'last_sync': fields.Datetime.now(),
        })
        if self.odoo_order_id:
          status_vals = self._prepare_sale_order_status_vals(order_data)
          status_vals['shopify_fulfillment_status'] = 'fulfilled'
          self.odoo_order_id._set_shopify_status_values(status_vals)

        # Log the fulfillment
        self.env['shopify.log'].create({
            'name': f'Fulfillment Created - Order {self.shopify_order_id}',
            'log_type': 'info',
            'message': f'Fulfillment created successfully for order {self.shopify_order_id}',
        })

        return True
      else:
        raise UserError(_(f'Failed to create fulfillment: {response.status_code} - {response.text}'))

    except Exception as e:
      self.env['shopify.log'].create({
          'name': f'Fulfillment Error - Order {self.shopify_order_id}',
          'log_type': 'error',
          'message': f'Error creating fulfillment: {str(e)}',
      })
      raise UserError(_(f'Error creating fulfillment: {str(e)}'))

  def sync_order_to_shopify(self, instance):
    """Bi-directional sync: Update order in Shopify with latest Odoo data"""
    self.ensure_one()

    if not self.shopify_order_id:
      _logger.warning(f"Cannot sync order {self.odoo_order_id.name} - no Shopify order ID")
      return False

    # Note: Shopify has limited order update capabilities after creation
    # We can update tags, notes, and some other fields

    url = f"{instance.shop_url}/admin/api/2024-07/orders/{self.shopify_order_id}.json"

    odoo_order = self.odoo_order_id

    update_data = {
        'order': {
            'id': int(self.shopify_order_id),
            'note': odoo_order.note or f"Updated from Odoo Order {odoo_order.name}",
            'tags': f"odoo,{odoo_order.state}",
        }
    }

    try:
      headers = {'Content-Type': 'application/json'}
      if hasattr(instance, 'access_token') and instance.access_token:
        headers['X-Shopify-Access-Token'] = instance.access_token
        response = requests.put(url, headers=headers, json=update_data, timeout=30)
      else:
        response = requests.put(url,
                                auth=(instance.api_key, instance.password),
                                json=update_data,
                                timeout=30)

      if response.status_code == 200:
        self.write({
            'sync_status': 'synced',
            'last_sync': fields.Datetime.now(),
        })
        _logger.info(f"Successfully synced order {odoo_order.name} to Shopify")
        return True
      else:
        _logger.error(
            f"Failed to sync order {odoo_order.name}: {response.status_code} - {response.text}")
        self.write({'sync_status': 'error'})
        return False
    except Exception as e:
      _logger.error(f"Exception syncing order {odoo_order.name}: {str(e)}")
      self.write({'sync_status': 'error'})
      return False

  def sync_order_from_shopify(self, instance):
    """Bi-directional sync: Update Odoo order with latest Shopify data"""
    self.ensure_one()

    if not self.shopify_order_id:
      _logger.warning(f"Cannot sync order {self.odoo_order_id.name} - no Shopify order ID")
      return False

    url = f"{instance.shop_url}/admin/api/2024-07/orders/{self.shopify_order_id}.json"

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
            'shopify_order_status':
                shopify_order.get('financial_status', 'pending'),
            'shopify_fulfillment_status':
                shopify_order.get('fulfillment_status', 'unfulfilled') or 'unfulfilled',
            'sync_status':
                'synced',
            'last_sync':
                fields.Datetime.now(),
        }

        # Update refund information if available
        refunds = shopify_order.get('refunds', [])
        if refunds:
          total_refund = sum([
              float(refund.get('transactions', [{}])[0].get('amount', 0))
              for refund in refunds
              if refund.get('transactions')
          ])
          if total_refund > 0:
            mapping_vals['shopify_refund_amount'] = total_refund
            if not self.shopify_refund_date:
              mapping_vals['shopify_refund_date'] = fields.Datetime.now()

        # Update fulfillment information if available
        fulfillments = shopify_order.get('fulfillments', [])
        if fulfillments:
          fulfillment = fulfillments[0]
          mapping_vals['shopify_fulfillment_id'] = str(fulfillment.get('id', ''))
          mapping_vals['carrier'] = fulfillment.get('tracking_company', '')
          mapping_vals['tracking_number'] = fulfillment.get('tracking_number', '')

          tracking_urls = fulfillment.get('tracking_urls', [])
          if tracking_urls:
            mapping_vals['tracking_url'] = tracking_urls[0]
          elif fulfillment.get('tracking_url'):
            mapping_vals['tracking_url'] = fulfillment.get('tracking_url')

        self.write(mapping_vals)

        if self.odoo_order_id:
          status_vals = self._prepare_sale_order_status_vals(shopify_order)
          self.odoo_order_id._set_shopify_status_values(status_vals)

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

  def action_process_refund(self):
    """Action to process refund from Odoo UI"""
    self.ensure_one()
    return {
        'name': 'Process Refund in Shopify',
        'type': 'ir.actions.act_window',
        'res_model': 'shopify.refund.wizard',
        'view_mode': 'form',
        'target': 'new',
        'context': {
            'default_order_mapping_id': self.id,
            'default_instance_id': self.instance_id.id,
        }
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
